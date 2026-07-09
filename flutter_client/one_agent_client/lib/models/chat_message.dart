import 'package:freezed_annotation/freezed_annotation.dart';

part 'chat_message.freezed.dart';
part 'chat_message.g.dart';

/// 聊天消息角色
enum MessageRole { user, assistant, system, thinking, tool }

/// 聊天消息模型
@freezed
class ChatMessage with _$ChatMessage {
  const factory ChatMessage({
    required String id,
    required MessageRole role,
    required String content,
    String? thinking,
    String? sessionId,
    DateTime? timestamp,
    bool? isStreaming,
    bool? isError,
    String? errorMessage,
    Map<String, dynamic>? metadata,
  }) = _ChatMessage;

  factory ChatMessage.fromJson(Map<String, dynamic> json) =>
      _$ChatMessageFromJson(json);

  const ChatMessage._();

  /// 从 API 响应创建助手消息
  factory ChatMessage.fromApiResponse(Map<String, dynamic> json, String sessionId) {
    return ChatMessage(
      id: DateTime.now().millisecondsSinceEpoch.toString(),
      role: MessageRole.assistant,
      content: json['reply'] ?? json['text'] ?? '',
      thinking: json['thinking'] as String?,
      sessionId: sessionId,
      timestamp: DateTime.now(),
      isStreaming: false,
    );
  }

  /// 创建用户消息
  factory ChatMessage.user({required String content, String? sessionId}) {
    return ChatMessage(
      id: DateTime.now().millisecondsSinceEpoch.toString(),
      role: MessageRole.user,
      content: content,
      sessionId: sessionId,
      timestamp: DateTime.now(),
    );
  }

  /// 创建思考过程消息
  factory ChatMessage.thinking({required String content}) {
    return ChatMessage(
      id: 'thinking_${DateTime.now().millisecondsSinceEpoch}',
      role: MessageRole.thinking,
      content: content,
      timestamp: DateTime.now(),
    );
  }

  /// 创建工具调用消息
  factory ChatMessage.tool({required String content, required String toolName}) {
    return ChatMessage(
      id: 'tool_${DateTime.now().millisecondsSinceEpoch}',
      role: MessageRole.tool,
      content: content,
      timestamp: DateTime.now(),
      metadata: {'tool': toolName},
    );
  }
}

/// SSE 流式事件
@freezed
class StreamEvent with _$StreamEvent {
  const factory StreamEvent({
    required String type,
    String? content,
    String? status,
    String? sessionId,
    bool? done,
    Map<String, dynamic>? metadata,
  }) = _StreamEvent;

  factory StreamEvent.fromJson(Map<String, dynamic> json) =>
      _$StreamEventFromJson(json);
}
