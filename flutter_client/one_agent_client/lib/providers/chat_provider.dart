import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/chat_api.dart';
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

  /// 设置当前会话
  void setSession(String? sessionId) {
    state = state.copyWith(currentSessionId: sessionId, messages: const []);
  }

  /// 加载会话历史（如果有 API 支持）
  Future<void> loadHistory(List<Map<String, dynamic>> history) async {
    final msgs = history.map((m) {
      final roleStr = m['role'] as String? ?? 'assistant';
      return ChatMessage(
        id: m['id'] ?? DateTime.now().millisecondsSinceEpoch.toString(),
        role: MessageRole.values.firstWhere(
          (r) => r.name == roleStr,
          orElse: () => MessageRole.assistant,
        ),
        content: m['content'] ?? '',
        thinking: m['thinking'] as String?,
        sessionId: state.currentSessionId,
        timestamp: m['timestamp'] != null
            ? DateTime.fromMillisecondsSinceEpoch(m['timestamp'])
            : DateTime.now(),
      );
    }).toList();
    state = state.copyWith(messages: msgs);
  }

  /// 发送消息（流式）
  Future<void> sendMessage(String text) async {
    if (text.trim().isEmpty) return;

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
    _streamSub?.cancel();
    final buffer = StringBuffer();
    String? thinkingBuffer;

    _streamSub = ChatApi.sendMessageStream(
      text: text,
      sessionId: state.currentSessionId,
    ).listen(
      (event) {
        if (event.type == 'thinking') {
          thinkingBuffer ??= '';
          thinkingBuffer = '${thinkingBuffer ?? ''}\n[思考中...]';
        } else if (event.type == 'text' && event.content != null) {
          buffer.write(event.content);
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
      },
    );
  }

  /// 取消当前流式请求
  void cancelStream() {
    _streamSub?.cancel();
    _streamSub = null;
    state = state.copyWith(isLoading: false);
  }

  /// 清空消息
  void clear() {
    _streamSub?.cancel();
    _streamSub = null;
    state = const ChatState();
  }

  @override
  void dispose() {
    _streamSub?.cancel();
    super.dispose();
  }
}

final chatProvider = StateNotifierProvider<ChatNotifier, ChatState>(
  (ref) => ChatNotifier(),
);
