// GENERATED CODE - DO NOT MODIFY BY HAND

part of 'approval.dart';

// **************************************************************************
// JsonSerializableGenerator
// **************************************************************************

_$ApprovalRequestImpl _$$ApprovalRequestImplFromJson(
        Map<String, dynamic> json) =>
    _$ApprovalRequestImpl(
      id: json['id'] as String,
      operation: json['operation'] as String,
      details: json['details'] as String?,
      riskLevel: json['riskLevel'] as String?,
      createdAt: json['createdAt'] == null
          ? null
          : DateTime.parse(json['createdAt'] as String),
      status: json['status'] as String?,
    );

Map<String, dynamic> _$$ApprovalRequestImplToJson(
        _$ApprovalRequestImpl instance) =>
    <String, dynamic>{
      'id': instance.id,
      'operation': instance.operation,
      'details': instance.details,
      'riskLevel': instance.riskLevel,
      'createdAt': instance.createdAt?.toIso8601String(),
      'status': instance.status,
    };
