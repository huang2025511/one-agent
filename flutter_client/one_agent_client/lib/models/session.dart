import 'package:freezed_annotation/freezed_annotation.dart';

part 'session.freezed.dart';
part 'session.g.dart';

/// 会话模型
@freezed
class Session with _$Session {
  const factory Session({
    required String id,
    String? title,
    DateTime? createdAt,
    DateTime? updatedAt,
    int? messageCount,
    String? status,
    String? source,
    Map<String, dynamic>? metadata,
  }) = _Session;

  factory Session.fromJson(Map<String, dynamic> json) =>
      _$SessionFromJson(json);

  const Session._();

  /// 从 API 列表项创建
  factory Session.fromApiList(Map<String, dynamic> json) {
    return Session(
      id: json['session_id'] ?? json['id'] ?? '',
      title: json['title'] ?? '未命名会话',
      createdAt: _parseTimestamp(json['created_at']),
      updatedAt: _parseTimestamp(json['updated_at']),
      messageCount: json['message_count'] ?? 0,
      status: json['status'] ?? 'active',
      source: json['source'],
    );
  }

  static DateTime? _parseTimestamp(dynamic value) {
    if (value == null) return null;
    if (value is int) {
      // 秒级时间戳转毫秒
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
}

/// 会话详情
@freezed
class SessionDetail with _$SessionDetail {
  const factory SessionDetail({
    required String id,
    required List<Map<String, dynamic>> messages,
    DateTime? createdAt,
    Map<String, dynamic>? metadata,
  }) = _SessionDetail;

  factory SessionDetail.fromJson(Map<String, dynamic> json) =>
      _$SessionDetailFromJson(json);
}
