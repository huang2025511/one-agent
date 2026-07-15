import 'package:dio/dio.dart';

import '../models/session.dart';
import 'api_client.dart';

/// 会话管理 API
class SessionApi {
  /// 获取会话列表
  static Future<List<Session>> listSessions({int limit = 50, int offset = 0}) async {
    final resp = await ApiClient.dio.get(
      '/api/sessions',
      queryParameters: {'limit': limit, 'offset': offset},
    );
    final data = resp.data as Map<String, dynamic>;
    final sessions = (data['sessions'] as List<dynamic>? ?? [])
        .map((e) => Session.fromApiList(e as Map<String, dynamic>))
        .toList();
    return sessions;
  }

  /// 获取单个会话
  static Future<SessionDetail?> getSession(String sessionId) async {
    try {
      final resp = await ApiClient.dio.get('/api/sessions/$sessionId');
      final data = resp.data as Map<String, dynamic>;
      return SessionDetail(
        id: sessionId,
        messages: (data['messages'] as List<dynamic>? ?? [])
            .map((e) => e as Map<String, dynamic>)
            .toList(),
        createdAt: _parseTimestamp(data['created_at']),
      );
    } on DioException catch (e) {
      if (e.response?.statusCode == 404) return null;
      rethrow;
    }
  }

  /// 解析服务端时间戳（float epoch 或 int 或 ISO 字符串）
  static DateTime? _parseTimestamp(dynamic value) {
    if (value == null) return null;
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
    return DateTime.tryParse(value.toString());
  }

  /// 删除会话
  static Future<bool> deleteSession(String sessionId) async {
    try {
      await ApiClient.dio.delete('/api/sessions/$sessionId');
      return true;
    } catch (_) {
      return false;
    }
  }

  /// Fork 会话
  static Future<String?> forkSession(String sessionId, {int forkPoint = 0}) async {
    try {
      final resp = await ApiClient.dio.post(
        '/api/sessions/$sessionId/fork',
        data: {'fork_point': forkPoint},
      );
      return (resp.data as Map<String, dynamic>)['new_session_id'] as String?;
    } catch (_) {
      return null;
    }
  }

  /// 批量删除会话
  static Future<Map<String, dynamic>?> batchDeleteSessions(List<String> sessionIds) async {
    try {
      final resp = await ApiClient.dio.post('/api/sessions/batch_delete', data: {
        'session_ids': sessionIds,
      });
      return resp.data as Map<String, dynamic>?;
    } catch (_) {
      return null;
    }
  }

  /// 获取会话消息列表
  static Future<Map<String, dynamic>?> getSessionMessages(String sessionId, {int limit = 100}) async {
    try {
      final resp = await ApiClient.dio.get('/api/sessions/$sessionId/messages', queryParameters: {'limit': limit});
      return resp.data as Map<String, dynamic>?;
    } catch (_) {
      return null;
    }
  }
}
