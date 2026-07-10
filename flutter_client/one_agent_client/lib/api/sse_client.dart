import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';

import '../models/chat_message.dart';
import '../utils/constants.dart';

/// SSE (Server-Sent Events) 客户端 — 用于流式聊天
class SseClient {
  final String baseUrl;
  final String? apiKey;
  HttpClient? _client;
  StreamSubscription? _subscription;

  SseClient({required this.baseUrl, this.apiKey});

  /// 发送流式聊天请求，返回 Stream<StreamEvent>
  Stream<StreamEvent> chatStream({
    required String text,
    String? sessionId,
    String? model,
    double? temperature,
    int? maxTokens,
  }) async* {
    _client?.close();
    _client = HttpClient();

    final uri = Uri.parse('$baseUrl${ApiConstants.chatStream}');
    final request = await _client!.postUrl(uri);

    request.headers.set('Content-Type', 'application/json');
    request.headers.set('Accept', 'text/event-stream');
    if (apiKey != null && apiKey!.isNotEmpty) {
      request.headers.set('X-API-Key', apiKey!);
    }

    final body = jsonEncode({
      'text': text,
      'session_id': sessionId,
      if (model != null) 'model': model,
      if (temperature != null) 'temperature': temperature,
      if (maxTokens != null) 'max_tokens': maxTokens,
    });
    request.write(body);

    final response = await request.close();

    if (response.statusCode != 200) {
      throw Exception('SSE error: ${response.statusCode}');
    }

    String buffer = '';
    await for (final chunk in response.transform(utf8.decoder)) {
      buffer += chunk;

      // SSE 格式: data: {...}\n\n
      while (buffer.contains('\n\n')) {
        final idx = buffer.indexOf('\n\n');
        final eventBlock = buffer.substring(0, idx);
        buffer = buffer.substring(idx + 2);

        final event = _parseSseBlock(eventBlock);
        if (event != null) {
          yield event;

          // 检测到完成事件时结束
          if (event.done == true) {
            _client?.close();
            _client = null;
            return;
          }
        }
      }
    }

    _client?.close();
    _client = null;
  }

  /// 解析 SSE 数据块
  StreamEvent? _parseSseBlock(String block) {
    final lines = block.split('\n');
    String? dataLine;
    String? eventType;

    for (final line in lines) {
      if (line.startsWith('data: ')) {
        dataLine = line.substring(6);
      } else if (line.startsWith('event: ')) {
        eventType = line.substring(7);
      }
    }

    if (dataLine == null) return null;

    try {
      final json = jsonDecode(dataLine) as Map<String, dynamic>;

      // 错误事件：服务端可能返回 {"error": "...", "done": true}
      if (json.containsKey('error') && json['error'] != null) {
        final errorMsg = json['error'].toString();
        if (errorMsg.isNotEmpty) {
          return StreamEvent(
            type: 'error',
            content: errorMsg,
            done: json['done'] == true,
            sessionId: json['session_id'] as String?,
            metadata: json,
          );
        }
      }

      // 处理不同格式的 SSE 事件
      if (json.containsKey('done') && json['done'] == true) {
        return StreamEvent(type: 'done', done: true, sessionId: json['session_id'] as String?);
      }

      if (json.containsKey('status') && json['status'] == 'thinking') {
        return StreamEvent(
          type: 'thinking',
          status: 'thinking',
          content: (json['content'] ?? json['text'] ?? json['thinking']) as String?,
          sessionId: json['session_id'] as String?,
        );
      }

      // 内容事件：识别 content / text / delta 三种字段名
      // 服务端 LLM 层用 delta，Coordinator 层用 content，兼容两者
      final content = json['content'] ?? json['text'] ?? json['delta'];
      if (content != null && content.toString().isNotEmpty) {
        return StreamEvent(
          type: eventType ?? 'text',
          content: content.toString(),
          sessionId: json['session_id'] as String?,
          metadata: json,
        );
      }

      return StreamEvent(type: eventType ?? 'unknown', metadata: json);
    } catch (e) {
      if (kDebugMode) debugPrint('SSE parse error: $e, data: $dataLine');
      return null;
    }
  }

  /// 取消流式请求
  void cancel() {
    _client?.close();
    _client = null;
    _subscription?.cancel();
    _subscription = null;
  }

  void dispose() {
    cancel();
  }
}
