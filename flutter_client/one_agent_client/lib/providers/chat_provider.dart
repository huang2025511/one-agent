import 'dart:async';
import 'package:flutter/foundation.dart';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/chat_api.dart';
import '../api/session_api.dart';
import '../api/sse_client.dart';
import '../models/chat_message.dart';

/// 聊天状态
class ChatState {
  final List<ChatMessage> messages;
  final bool isLoading;
  final String? error;
  final String? currentSessionId;

  const ChatState({
    this.messages = const [],
    this.isLoading = false,
    this.error,
    this.currentSessionId,
  });

  ChatState copyWith({
    List<ChatMessage>? messages,
    bool? isLoading,
    String? error,
    String? currentSessionId,
  }) => ChatState(
    messages: messages ?? this.messages,
    isLoading: isLoading ?? this.isLoading,
    error: error,
    currentSessionId: currentSessionId ?? this.currentSessionId,
  );
}

/// 聊天 Provider
class ChatNotifier extends StateNotifier<ChatState> {
  ChatNotifier() : super(const ChatState());

  StreamSubscription<StreamEvent>? _streamSub;
  SseClient? _sseClient;

  /// 释放当前进行中的流式请求
  void _disposeStream() {
    _streamSub?.cancel();
    _streamSub = null;
    _sseClient?.dispose();
    _sseClient = null;
  }

  /// 设置当前会话，并加载该会话的历史消息
  Future<void> setSession(String? sessionId) async {
    _disposeStream();

    state = state.copyWith(
      currentSessionId: sessionId,
      messages: const [],
      error: null,
      isLoading: sessionId != null,
    );

    if (sessionId == null) return;

    try {
      final detail = await SessionApi.getSession(sessionId);
      // 加载期间用户可能又切换了会话，需校验
      if (state.currentSessionId != sessionId) return;
      if (detail != null) {
        await loadHistory(detail.messages);
      }
    } catch (e) {
      if (state.currentSessionId == sessionId) {
        state = state.copyWith(error: '加载会话历史失败: $e');
      }
    } finally {
      if (state.currentSessionId == sessionId) {
        state = state.copyWith(isLoading: false);
      }
    }
  }

  /// 加载会话历史
  Future<void> loadHistory(List<Map<String, dynamic>> history) async {
    final msgs = history.map((m) {
      final roleStr = m['role'] as String? ?? 'assistant';
      // 服务端消息结构: {id, session_id, role, content, meta, created_at, tokens}
      // thinking 嵌套在 meta 中（如果有），created_at 是 float epoch
      final meta = m['meta'];
      final thinking = meta is Map ? meta['thinking'] as String? : null;
      return ChatMessage(
        id: m['id']?.toString() ??
            'hist_${DateTime.now().millisecondsSinceEpoch}_${m['role'] ?? 'msg'}',
        role: MessageRole.values.firstWhere(
          (r) => r.name == roleStr,
          orElse: () => MessageRole.assistant,
        ),
        content: (m['content'] ?? m['text'] ?? m['reply'] ?? '').toString(),
        thinking: thinking,
        sessionId: state.currentSessionId,
        timestamp: _parseTimestamp(m['created_at'] ?? m['timestamp']),
      );
    }).toList();
    state = state.copyWith(messages: msgs);
  }

  /// 解析服务端时间戳（float/int epoch 或 ISO 字符串）
  DateTime _parseTimestamp(dynamic value) {
    if (value == null) return DateTime.now();
    if (value is int) {
      return DateTime.fromMillisecondsSinceEpoch(
        value > 1e12 ? value : value * 1000,
      );
    }
    if (value is double) {
      return DateTime.fromMillisecondsSinceEpoch(
        (value > 1e12 ? value : value * 1000).toInt(),
      );
    }
    return DateTime.tryParse(value.toString()) ?? DateTime.now();
  }

  /// 发送消息（流式）
  Future<void> sendMessage(String text, {String? language}) async {
    if (text.trim().isEmpty) return;

    // 取消上一个进行中的流式请求，并标记上一条助手消息结束流式
    _disposeStream();
    final prevMsgs = [...state.messages];
    final lastIdx = prevMsgs.length - 1;
    if (lastIdx >= 0 && prevMsgs[lastIdx].role == MessageRole.assistant) {
      prevMsgs[lastIdx] = prevMsgs[lastIdx].copyWith(isStreaming: false);
    }

    // 添加用户消息（使用已更新上一条流式状态的 prevMsgs）
    final userMsg = ChatMessage.user(
      content: text,
      sessionId: state.currentSessionId,
    );
    state = state.copyWith(
      messages: [...prevMsgs, userMsg],
      isLoading: true,
      error: null,
    );

    // 创建占位助手消息
    final assistantMsg = ChatMessage(
      id: 'assistant_${DateTime.now().millisecondsSinceEpoch}',
      role: MessageRole.assistant,
      content: '',
      sessionId: state.currentSessionId,
      timestamp: DateTime.now(),
      isStreaming: true,
    );
    state = state.copyWith(messages: [...state.messages, assistantMsg]);

    // 开始 SSE 流式接收
    final buffer = StringBuffer();
    String? thinkingBuffer;
    // 追踪 phase 顺序用于生成摘要时间线
    final List<String> _phaseOrder = [];
    String? _lastPhase;

    StreamChatResult result;
    try {
      result = ChatApi.sendMessageStream(
        text: text,
        sessionId: state.currentSessionId,
        language: language,
      );
    } catch (e) {
      debugPrint('❌ sendMessageStream 创建失败: $e');
      _updateLastAssistantMessage(
        content: '创建请求失败: $e',
        isError: true,
        errorMessage: '创建请求失败: $e',
      );
      state = state.copyWith(isLoading: false, error: '创建请求失败: $e');
      return;
    }
    _sseClient = result.client;

    _streamSub = result.stream.listen(
      (event) {
        // 修复：dispose 后 stream 回调可能仍在飞，state 写入会抛异常
        if (_streamSub == null) return;
        // 错误事件：显示错误信息，不写入回复内容
        if (event.type == 'error') {
          final updatedMsgs = [...state.messages];
          final lastIdx = updatedMsgs.length - 1;
          if (lastIdx >= 0 && updatedMsgs[lastIdx].role == MessageRole.assistant) {
            updatedMsgs[lastIdx] = updatedMsgs[lastIdx].copyWith(
              content: event.content ?? '发生未知错误',
              isError: true,
              errorMessage: event.content,
              isStreaming: false,
            );
            state = state.copyWith(messages: updatedMsgs, isLoading: false);
          }
          return;
        }

        if (event.type == 'thinking') {
          final content = event.content ?? '';
          final phase = event.phase ?? 'thinking';
          if (event.phase == 'plan') {
            // phase=plan 是最终完整思考计划，覆盖之前截断的进度版
            // 追加换行符确保下一段 phase 标题能正确分行
            thinkingBuffer = content.isEmpty ? null : content + '\n\n';
            if (!_phaseOrder.contains('plan')) _phaseOrder.add('plan');
            // 同步更新 _last_phase，plan 之后回到新 phase 时能正确生成标题
            _lastPhase = 'plan';
          } else if (content.isEmpty) {
            // 初始 thinking 占位事件，无 content，忽略
          } else {
            // 按 phase 分类格式化：每个 phase 首次出现时添加标题行
            if (_lastPhase != phase) {
              _lastPhase = phase;
              if (!_phaseOrder.contains(phase)) _phaseOrder.add(phase);
              final icon = _phaseIcon(phase);
              final label = _phaseLabel(phase);
              thinkingBuffer = (thinkingBuffer ?? '') + '### $icon $label\n';
            }
            thinkingBuffer = (thinkingBuffer ?? '') + '$content\n\n';
          }
        } else if (event.content != null) {
          buffer.write(event.content);
        }

        // 更新最后一条消息
        final updatedMsgs = [...state.messages];
        final lastIdx = updatedMsgs.length - 1;
        if (lastIdx >= 0 && updatedMsgs[lastIdx].role == MessageRole.assistant) {
          updatedMsgs[lastIdx] = updatedMsgs[lastIdx].copyWith(
            content: buffer.toString(),
            thinking: thinkingBuffer,
            metadata: {
              if (updatedMsgs[lastIdx].metadata != null)
                ...updatedMsgs[lastIdx].metadata!,
              'thinkingSummary': _phaseOrder
                  .map((p) => '${_phaseIcon(p)} ${_phaseLabel(p)}')
                  .join(' → '),
            },
          );
          state = state.copyWith(messages: updatedMsgs);
        }

        // 完成
        if (event.done == true) {
          // 修复：无论是否有 sessionId，done 事件都应结束流式状态
          // 否则若服务端漏发 sessionId，isLoading 会永久卡住
          state = state.copyWith(
            currentSessionId: event.sessionId ?? state.currentSessionId,
            isLoading: false,
          );
        }
      },
      onError: (err) {
        debugPrint('❌ SSE stream onError: $err');
        // 标记占位助手消息结束流式并显示错误
        final updatedMsgs = [...state.messages];
        final lastIdx = updatedMsgs.length - 1;
        if (lastIdx >= 0 && updatedMsgs[lastIdx].role == MessageRole.assistant) {
          updatedMsgs[lastIdx] = updatedMsgs[lastIdx].copyWith(
            content: '发送失败: $err',
            isStreaming: false,
            isError: true,
            errorMessage: err.toString(),
          );
        }
        state = state.copyWith(
          messages: updatedMsgs,
          isLoading: false,
          error: '发送失败: $err',
        );
      },
      onDone: () {
        // 标记流结束
        final updatedMsgs = [...state.messages];
        final lastIdx = updatedMsgs.length - 1;
        if (lastIdx >= 0 && updatedMsgs[lastIdx].role == MessageRole.assistant) {
          updatedMsgs[lastIdx] = updatedMsgs[lastIdx].copyWith(isStreaming: false);
        }
        state = state.copyWith(messages: updatedMsgs, isLoading: false);
        _sseClient?.dispose();
        _sseClient = null;
      },
    );
  }

  /// 更新最后一条助手消息
  void _updateLastAssistantMessage({
    String? content,
    bool? isStreaming,
    bool? isError,
    String? errorMessage,
    String? thinking,
  }) {
    final updatedMsgs = [...state.messages];
    final lastIdx = updatedMsgs.length - 1;
    if (lastIdx >= 0 && updatedMsgs[lastIdx].role == MessageRole.assistant) {
      final oldMsg = updatedMsgs[lastIdx];
      updatedMsgs[lastIdx] = oldMsg.copyWith(
        content: content ?? oldMsg.content,
        isStreaming: isStreaming,
        isError: isError,
        errorMessage: errorMessage,
        thinking: thinking,
      );
      state = state.copyWith(messages: updatedMsgs);
    }
  }

  /// 取消当前流式请求
  void cancelStream() {
    _disposeStream();
    // 标记最后一条助手消息结束流式
    final updatedMsgs = [...state.messages];
    final lastIdx = updatedMsgs.length - 1;
    if (lastIdx >= 0 && updatedMsgs[lastIdx].role == MessageRole.assistant) {
      updatedMsgs[lastIdx] = updatedMsgs[lastIdx].copyWith(isStreaming: false);
    }
    state = state.copyWith(messages: updatedMsgs, isLoading: false);
  }

  /// 清空消息
  void clear() {
    _disposeStream();
    state = const ChatState();
  }

  @override
  void dispose() {
    _disposeStream();
    super.dispose();
  }

  /// Phase → 图标映射
  static String _phaseIcon(String phase) {
    switch (phase) {
      case 'planning':
      case 'plan':
        return '📋';
      case 'thinking':
        return '💭';
      case 'reflection':
        return '🔄';
      case 'reasoning':
        return '🧠';
      case 'tool_loop':
        return '🔧';
      case 'tool_result':
        return '📊';
      case 'skill_dispatch':
        return '⚡';
      case 'streaming':
        return '💬';
      case 'verification':
        return '✅';
      case 'regeneration':
        return '♻️';
      case 'rewrite':
        return '✏️';
      case 'agent_mesh':
        return '🕸️';
      case 'multi_agent':
        return '🤝';
      case 'deep_research':
        return '🔍';
      case 'comparison':
        return '⚖️';
      case 'chart':
        return '📈';
      case 'batch':
        return '📦';
      case 'eval':
        return '📝';
      case 'model_compare':
        return '🔀';
      case 'provider_resolve':
        return '🔌';
      default:
        return '•';
    }
  }

  /// Phase → 中文标签映射
  static String _phaseLabel(String phase) {
    switch (phase) {
      case 'planning':
        return '规划中';
      case 'plan':
        return '执行计划';
      case 'thinking':
        return '思考中';
      case 'reflection':
        return '反思';
      case 'reasoning':
        return '推理';
      case 'tool_loop':
        return '工具调用';
      case 'tool_result':
        return '工具结果';
      case 'skill_dispatch':
        return '技能调度';
      case 'streaming':
        return '生成回复';
      case 'verification':
        return '验证结果';
      case 'regeneration':
        return '重新生成';
      case 'rewrite':
        return '重写优化';
      case 'agent_mesh':
        return 'Agent网格';
      case 'multi_agent':
        return '多Agent协作';
      case 'deep_research':
        return '深度研究';
      case 'comparison':
        return '方案比较';
      case 'chart':
        return '图表生成';
      case 'batch':
        return '批量处理';
      case 'eval':
        return '评估';
      case 'model_compare':
        return '模型比较';
      case 'provider_resolve':
        return '服务商解析';
      default:
        return phase;
    }
  }
}

final chatProvider = StateNotifierProvider<ChatNotifier, ChatState>(
  (ref) => ChatNotifier(),
);
