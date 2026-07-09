import 'package:dio/dio.dart';

import '../models/chat_message.dart';
import 'api_client.dart';
import 'sse_client.dart';

/// 聊天相关 API
class ChatApi {
  /// 非流式聊天
  static Future<Map<String, dynamic>> sendMessage({
    required String text,
    String? sessionId,
  }) async {
    final resp = await ApiClient.dio.post(
      '/api/chat',
      data: {'text': text, 'session_id': sessionId},
    );
    return resp.data as Map<String, dynamic>;
  }

  /// 流式聊天 — 返回 SSE 事件流
  static Stream<StreamEvent> sendMessageStream({
    required String text,
    String? sessionId,
    String? model,
    double? temperature,
    int? maxTokens,
  }) {
    final sse = SseClient(
      baseUrl: ApiClient.dio.options.baseUrl,
      apiKey: ApiClient.dio.options.headers['X-API-Key'] as String?,
    );
    return sse.chatStream(
      text: text,
      sessionId: sessionId,
      model: model,
      temperature: temperature,
      maxTokens: maxTokens,
    );
  }
}
