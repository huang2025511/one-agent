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
      createdAt: json['created_at'] != null
          ? DateTime.tryParse(json['created_at'].toString())
          : null,
      status: json['status'] ?? 'pending',
    );
  }

  bool get isPending => status == 'pending';
  bool get isHighRisk => riskLevel == 'high';
}
