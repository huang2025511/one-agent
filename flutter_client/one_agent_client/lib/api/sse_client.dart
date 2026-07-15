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

  /// 最大重试次数（仅连接建立阶段）
  static const int _maxRetries = 3;

  /// 重试初始延迟（毫秒）
  static const int _retryBaseDelayMs = 1000;

  /// 流超时：超过此时间未收到任何事件则视为断线
  /// 设为 120 秒，因为 LLM 429 限流回退非流式时可能需要 60-120 秒
  /// 配合服务端 10 秒心跳保活，正常情况下不会触发此超时
  static const Duration _streamIdleTimeout = Duration(seconds: 120);

  SseClient({required this.baseUrl, this.apiKey});

  /// 发送流式聊天请求，返回 Stream<StreamEvent>
  ///
  /// 断线重连策略：
  /// - 连接建立阶段：指数退避重试，最多 _maxRetries 次
  /// - 流传输阶段：不重连。流意外结束直接 yield error，避免重复 POST
  ///   同一消息导致服务端重复处理（双倍 token 成本、session 数据污染）。
  /// - 已收到内容后断线：yield 提示"内容可能不完整"的 error 事件
  Stream<StreamEvent> chatStream({
    required String text,
    String? sessionId,
    String? model,
    double? temperature,
    int? maxTokens,
    String? language,
  }) async* {
    _client?.close();
    _client = HttpClient();
    _client!.connectionTimeout = const Duration(seconds: 15);

    final cleanBaseUrl = baseUrl.replaceAll(RegExp(r'/+$'), '');
    final uri = Uri.parse('$cleanBaseUrl${ApiConstants.chatStream}');
    // 仅 debug 模式打印，避免 release 构建把用户消息前缀泄漏到系统日志
    if (kDebugMode) {
      debugPrint('🌐 SSE: POST $uri');
    }

    // 追踪是否已收到内容
    bool hasReceivedContent = false;

    for (int attempt = 0; attempt <= _maxRetries; attempt++) {
      if (attempt > 0) {
        final delay = _retryBaseDelayMs * (1 << (attempt - 1));
        if (kDebugMode) {
          debugPrint('🔄 SSE: 重连第 $attempt/$_maxRetries 次，等待 ${delay}ms...');
        }
        // 通知客户端正在重连
        yield StreamEvent(
          type: 'thinking',
          status: 'thinking',
          content: '网络波动，正在重新连接...',
          phase: 'reconnect',
        );
        await Future.delayed(Duration(milliseconds: delay));
        _client?.close();
        _client = HttpClient();
        _client!.connectionTimeout = const Duration(seconds: 15);
      }

      // ── 1. 建立连接 ──────────────────────────────────────
      late HttpClientRequest request;
      try {
        request = await _client!.postUrl(uri);
      } catch (e) {
        if (kDebugMode) {
          debugPrint('❌ SSE: 连接失败 (尝试 ${attempt + 1}/${_maxRetries + 1}) - $e');
        }
        if (attempt == _maxRetries) {
          yield StreamEvent(
            type: 'error',
            content: '连接失败: $e',
            done: true,
          );
          return;
        }
        continue;
      }

      request.headers.set('Content-Type', 'application/json');
      request.headers.set('Accept', 'text/event-stream');
      if (apiKey != null && apiKey!.isNotEmpty) {
        request.headers.set('X-API-Key', apiKey!);
      }

      final body = jsonEncode({
        'text': text,
        if (sessionId != null && sessionId.isNotEmpty) 'session_id': sessionId,
        if (model != null) 'model': model,
        if (temperature != null) 'temperature': temperature,
        if (maxTokens != null) 'max_tokens': maxTokens,
        if (language != null && language.isNotEmpty) 'language': language,
      });
      request.add(utf8.encode(body));

      HttpClientResponse response;
      try {
        response = await request.close();
      } catch (e) {
        if (kDebugMode) {
          debugPrint('❌ SSE: 请求发送失败 - $e');
        }
        if (attempt == _maxRetries) {
          yield StreamEvent(
            type: 'error',
            content: '请求发送失败: $e',
            done: true,
          );
          return;
        }
        continue;
      }

      if (kDebugMode) {
        debugPrint('✅ SSE: 响应状态 ${response.statusCode}');
      }

      if (response.statusCode != 200) {
        final responseBody = await response.transform(utf8.decoder).join();
        if (kDebugMode) {
          debugPrint('❌ SSE: 错误响应体 - $responseBody');
        }
        yield StreamEvent(
          type: 'error',
          content: '服务器错误: ${response.statusCode}',
          done: true,
        );
        return;
      }

      // ── 2. 读取流 ────────────────────────────────────────
      String buffer = '';
      bool receivedDone = false;

      try {
        await for (final chunk in response.transform(utf8.decoder).timeout(_streamIdleTimeout)) {
          buffer += chunk;

          while (buffer.contains('\n\n')) {
            final idx = buffer.indexOf('\n\n');
            final eventBlock = buffer.substring(0, idx);
            buffer = buffer.substring(idx + 2);

            final event = _parseSseBlock(eventBlock);
            if (event != null) {
              // 追踪是否收到实际内容（非 thinking 占位事件）
              if (!hasReceivedContent) {
                if (event.type == 'text' || event.type == 'done' ||
                    (event.type == 'thinking' && event.content != null && event.content!.isNotEmpty)) {
                  hasReceivedContent = true;
                }
              }

              yield event;

              if (event.done == true) {
                receivedDone = true;
                _client?.close();
                _client = null;
                return; // 正常结束
              }
            }
          }
        }
      } on TimeoutException {
        if (kDebugMode) {
          debugPrint('⏰ SSE: 流超时（${_streamIdleTimeout.inSeconds}秒无数据）');
        }
      } catch (e) {
        if (kDebugMode) {
          debugPrint('❌ SSE: 流读取中断 - $e');
        }
      }

      // ── 3. 流意外结束（未收到 done 事件）─────────────────
      if (!receivedDone) {
        if (kDebugMode) {
          debugPrint('⚠️ SSE: 流意外结束，hasReceivedContent=$hasReceivedContent');
        }

        if (hasReceivedContent) {
          // 已收到部分内容 → 内容可能不完整
          yield StreamEvent(
            type: 'error',
            content: '连接中断，已收到的内容可能不完整',
            done: true,
          );
        } else {
          // 未收到任何内容 → 超时或连接中断
          yield StreamEvent(
            type: 'error',
            content: '服务器响应超时，请稍后重试',
            done: true,
          );
        }
        _client?.close();
        _client = null;
        return;
      }
    }

    // 理论上不会到这里（循环内各分支都有 return）
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

      // 心跳事件：服务端保活信号，不显示给用户
      if (json.containsKey('status') && json['status'] == 'heartbeat') {
        return StreamEvent(
          type: 'heartbeat',
          sessionId: json['session_id'] as String?,
        );
      }

      if (json.containsKey('status') && json['status'] == 'thinking') {
        return StreamEvent(
          type: 'thinking',
          status: 'thinking',
          content: (json['content'] ?? json['text'] ?? json['thinking']) as String?,
          phase: json['phase'] as String?,
          sessionId: json['session_id'] as String?,
        );
      }

      // 内容事件：识别 content / text / delta 三种字段名
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
  }

  void dispose() {
    cancel();
  }
}