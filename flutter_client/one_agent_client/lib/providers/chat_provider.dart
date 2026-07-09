import 'dart:async';

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
      return ChatMessage(
        id: m['id']?.toString() ??
            'hist_${DateTime.now().millisecondsSinceEpoch}_${m['role'] ?? 'msg'}',
        role: MessageRole.values.firstWhere(
          (r) => r.name == roleStr,
          orElse: () => MessageRole.assistant,
        ),
        content: (m['content'] ?? m['text'] ?? m['reply'] ?? '').toString(),
        thinking: m['thinking'] as String?,
        sessionId: state.currentSessionId,
        timestamp: m['timestamp'] != null
            ? (m['timestamp'] is int
                ? DateTime.fromMillisecondsSinceEpoch(
                    (m['timestamp'] as int) > 1e12.toInt()
                        ? (m['timestamp'] as int)
                        : (m['timestamp'] as int) * 1000,
                  )
                : DateTime.tryParse(m['timestamp'].toString()) ?? DateTime.now())
            : DateTime.now(),
      );
    }).toList();
    state = state.copyWith(messages: msgs);
  }

  /// 发送消息（流式）
  Future<void> sendMessage(String text) async {
    if (text.trim().isEmpty) return;

    // 取消上一个进行中的流式请求
    _disposeStream();

    // 添加用户消息
    final userMsg = ChatMessage.user(
      content: text,
      sessionId: state.currentSessionId,
    );
    state = state.copyWith(
      messages: [...state.messages, userMsg],
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

    final result = ChatApi.sendMessageStream(
      text: text,
      sessionId: state.currentSessionId,
    );
    _sseClient = result.client;

    _streamSub = result.stream.listen(
      (event) {
        if (event.type == 'thinking') {
          // 累积真实的思考内容
          thinkingBuffer = (thinkingBuffer ?? '') + (event.content ?? '');
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
          );
          state = state.copyWith(messages: updatedMsgs);
        }

        // 完成
        if (event.done == true && event.sessionId != null) {
          state = state.copyWith(currentSessionId: event.sessionId);
        }
      },
      onError: (err) {
        state = state.copyWith(
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
}

final chatProvider = StateNotifierProvider<ChatNotifier, ChatState>(
  (ref) => ChatNotifier(),
);
