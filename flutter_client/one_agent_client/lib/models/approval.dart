import 'package:freezed_annotation/freezed_annotation.dart';

part 'approval.freezed.dart';
part 'approval.g.dart';

/// 审批请求
@freezed
class ApprovalRequest with _$ApprovalRequest {
  const factory ApprovalRequest({
    required String id,
    required String operation,
    String? details,
    String? riskLevel,
    DateTime? createdAt,
    String? status,
  }) = _ApprovalRequest;

  factory ApprovalRequest.fromJson(Map<String, dynamic> json) =>
      _$ApprovalRequestFromJson(json);

  const ApprovalRequest._();

  factory ApprovalRequest.fromApi(Map<String, dynamic> json) {
    return ApprovalRequest(
      id: json['id'] ?? json['request_id'] ?? '',
      operation: json['operation'] ?? '未知操作',
      details: json['details'],
      riskLevel: json['risk_level'] ?? 'medium',
      // 服务端 created_at 是 float epoch（time.time()），不是 ISO 字符串
      createdAt: _parseTimestamp(json['created_at']),
      status: json['status'] ?? 'pending',
    );
  }

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

  bool get isPending => status == 'pending';
  bool get isHighRisk => riskLevel == 'high';
}
