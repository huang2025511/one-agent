import '../models/chat_message.dart';
import 'api_client.dart';
import 'sse_client.dart';

/// 流式聊天的返回结果：包含事件流和底层 SseClient（调用方需在结束时 dispose）
class StreamChatResult {
  final Stream<StreamEvent> stream;
  final SseClient client;

  const StreamChatResult({required this.stream, required this.client});
}

/// 聊天相关 API
class ChatApi {
  /// 非流式聊天
  static Future<Map<String, dynamic>> sendMessage({
    required String text,
    String? sessionId,
  }) async {
    final resp = await ApiClient.dio.post(
      '/api/chat',
      data: {
        'text': text,
        if (sessionId != null && sessionId.isNotEmpty) 'session_id': sessionId,
      },
    );
    return resp.data as Map<String, dynamic>;
  }

  /// 流式聊天 — 返回 SSE 事件流及 SseClient（调用方负责 dispose）
  static StreamChatResult sendMessageStream({
    required String text,
    String? sessionId,
    String? model,
    double? temperature,
    int? maxTokens,
    String? language,
  }) {
    final sse = SseClient(
      baseUrl: ApiClient.baseUrl,
      apiKey: ApiClient.apiKey,
    );
    return StreamChatResult(
      stream: sse.chatStream(
        text: text,
        sessionId: sessionId,
        model: model,
        temperature: temperature,
        maxTokens: maxTokens,
        language: language,
      ),
      client: sse,
    );
  }
}
