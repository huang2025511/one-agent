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
      createdAt: json['created_at'] != null
          ? DateTime.fromMillisecondsSinceEpoch((json['created_at'] * 1000).toInt())
          : null,
      updatedAt: json['updated_at'] != null
          ? DateTime.fromMillisecondsSinceEpoch((json['updated_at'] * 1000).toInt())
          : null,
      messageCount: json['message_count'] ?? 0,
      status: json['status'] ?? 'active',
      source: json['source'],
    );
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
