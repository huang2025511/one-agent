// GENERATED CODE - DO NOT MODIFY BY HAND

part of 'system_stats.dart';

// **************************************************************************
// JsonSerializableGenerator
// **************************************************************************

_$SystemStatsImpl _$$SystemStatsImplFromJson(Map<String, dynamic> json) =>
    _$SystemStatsImpl(
      uptimeSeconds: (json['uptimeSeconds'] as num?)?.toInt(),
      busMetrics: json['busMetrics'] as Map<String, dynamic>?,
      llmStats: json['llmStats'] as Map<String, dynamic>?,
      memoryStats: json['memoryStats'] as Map<String, dynamic>?,
      skillsCount: (json['skillsCount'] as num?)?.toInt(),
      sessions: json['sessions'] as Map<String, dynamic>?,
      messages: json['messages'] as Map<String, dynamic>?,
      knowledgeGraph: json['knowledgeGraph'] as Map<String, dynamic>?,
    );

Map<String, dynamic> _$$SystemStatsImplToJson(_$SystemStatsImpl instance) =>
    <String, dynamic>{
      'uptimeSeconds': instance.uptimeSeconds,
      'busMetrics': instance.busMetrics,
      'llmStats': instance.llmStats,
      'memoryStats': instance.memoryStats,
      'skillsCount': instance.skillsCount,
      'sessions': instance.sessions,
      'messages': instance.messages,
      'knowledgeGraph': instance.knowledgeGraph,
    };

_$SystemHealthImpl _$$SystemHealthImplFromJson(Map<String, dynamic> json) =>
    _$SystemHealthImpl(
      status: json['status'] as String,
      uptime: (json['uptime'] as num?)?.toInt(),
      version: json['version'] as String?,
      components: json['components'] as Map<String, dynamic>?,
    );

Map<String, dynamic> _$$SystemHealthImplToJson(_$SystemHealthImpl instance) =>
    <String, dynamic>{
      'status': instance.status,
      'uptime': instance.uptime,
      'version': instance.version,
      'components': instance.components,
    };

_$CostStatsImpl _$$CostStatsImplFromJson(Map<String, dynamic> json) =>
    _$CostStatsImpl(
      daily: json['daily'] as Map<String, dynamic>?,
      byProvider: json['byProvider'] as Map<String, dynamic>?,
      byModel: json['byModel'] as Map<String, dynamic>?,
      totalTokens: (json['totalTokens'] as num?)?.toInt(),
      totalCost: (json['totalCost'] as num?)?.toDouble(),
    );

Map<String, dynamic> _$$CostStatsImplToJson(_$CostStatsImpl instance) =>
    <String, dynamic>{
      'daily': instance.daily,
      'byProvider': instance.byProvider,
      'byModel': instance.byModel,
      'totalTokens': instance.totalTokens,
      'totalCost': instance.totalCost,
    };

_$AppConfigImpl _$$AppConfigImplFromJson(Map<String, dynamic> json) =>
    _$AppConfigImpl(
      config: json['config'] as Map<String, dynamic>?,
      timestamp: json['timestamp'] == null
          ? null
          : DateTime.parse(json['timestamp'] as String),
    );

Map<String, dynamic> _$$AppConfigImplToJson(_$AppConfigImpl instance) =>
    <String, dynamic>{
      'config': instance.config,
      'timestamp': instance.timestamp?.toIso8601String(),
    };
