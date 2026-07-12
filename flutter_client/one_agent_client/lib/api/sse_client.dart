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

  /// 最大重试次数（连接建立失败时）
  static const int _maxConnectRetries = 3;

  /// 重试初始延迟（毫秒）
  static const int _retryBaseDelayMs = 1000;

  SseClient({required this.baseUrl, this.apiKey});

  /// 发送流式聊天请求，返回 Stream<StreamEvent>
  /// 连接失败时会自动重试（指数退避），最多重试 _maxConnectRetries 次
  Stream<StreamEvent> chatStream({
    required String text,
    String? sessionId,
    String? model,
    double? temperature,
    int? maxTokens,
  }) async* {
    _client?.close();
    _client = HttpClient();
    _client!.connectionTimeout = const Duration(seconds: 15);

    // 修复：处理 baseUrl 尾部斜杠，避免双斜杠导致 404
    final cleanBaseUrl = baseUrl.replaceAll(RegExp(r'/+$'), '');
    final uri = Uri.parse('$cleanBaseUrl${ApiConstants.chatStream}');
    debugPrint('🌐 SSE: POST $uri | baseUrl=$baseUrl | text=${text.substring(0, text.length > 30 ? 30 : text.length)}...');

    HttpClientRequest request;
    Exception? lastError;

    for (int attempt = 0; attempt <= _maxConnectRetries; attempt++) {
      if (attempt > 0) {
        final delay = _retryBaseDelayMs * (1 << (attempt - 1));
        debugPrint('🔄 SSE: 连接重试第 $attempt/$_maxConnectRetries 次，等待 ${delay}ms...');
        await Future.delayed(Duration(milliseconds: delay));
        // 每次重试前重建 client，避免旧连接状态干扰
        _client?.close();
        _client = HttpClient();
        _client!.connectionTimeout = const Duration(seconds: 15);
      }

      try {
        request = await _client!.postUrl(uri);
        break; // 连接成功，跳出重试循环
      } catch (e) {
        lastError = e is Exception ? e : Exception(e.toString());
        debugPrint('❌ SSE: 连接失败 (尝试 ${attempt + 1}/${_maxConnectRetries + 1}) - $e');
        if (attempt == _maxConnectRetries) {
          rethrow; // 最后一次重试失败，抛出异常
        }
      }
    }

    request.headers.set('Content-Type', 'application/json');
    request.headers.set('Accept', 'text/event-stream');
    if (apiKey != null && apiKey!.isNotEmpty) {
      request.headers.set('X-API-Key', apiKey!);
    }

    final body = jsonEncode({
      'text': text,
      // 只在有值时发送 session_id，避免发送 null 导致服务端
      // body.get("session_id", default) 返回 None 而非默认值
      if (sessionId != null && sessionId.isNotEmpty) 'session_id': sessionId,
      if (model != null) 'model': model,
      if (temperature != null) 'temperature': temperature,
      if (maxTokens != null) 'max_tokens': maxTokens,
    });
    request.write(body);

    HttpClientResponse response;
    try {
      response = await request.close();
    } catch (e) {
      debugPrint('❌ SSE: 请求失败 - $e');
      rethrow;
    }

    debugPrint('✅ SSE: 响应状态 ${response.statusCode}');

    if (response.statusCode != 200) {
      final responseBody = await response.transform(utf8.decoder).join();
      debugPrint('❌ SSE: 错误响应体 - $responseBody');
      throw Exception('SSE error: ${response.statusCode} - $responseBody');
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
