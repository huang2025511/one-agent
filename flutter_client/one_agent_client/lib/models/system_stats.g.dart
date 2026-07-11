// GENERATED CODE - DO NOT MODIFY BY HAND

part of 'system_stats.dart';

// **************************************************************************
// JsonSerializableGenerator
// **************************************************************************

_$SystemStatsImpl _$$SystemStatsImplFromJson(Map<String, dynamic> json) =>
    _$SystemStatsImpl(
      uptimeSeconds: (json['uptime_seconds'] as num?)?.toInt(),
      busMetrics: json['bus_metrics'] as Map<String, dynamic>?,
      llmStats: json['llm_stats'] as Map<String, dynamic>?,
      memoryStats: json['memory_stats'] as Map<String, dynamic>?,
      skillsCount: (json['skills_count'] as num?)?.toInt(),
      sessions: json['sessions'] as Map<String, dynamic>?,
      messages: json['messages'] as Map<String, dynamic>?,
      knowledgeGraph: json['knowledge_graph'] as Map<String, dynamic>?,
    );

Map<String, dynamic> _$$SystemStatsImplToJson(_$SystemStatsImpl instance) =>
    <String, dynamic>{
      'uptime_seconds': instance.uptimeSeconds,
      'bus_metrics': instance.busMetrics,
      'llm_stats': instance.llmStats,
      'memory_stats': instance.memoryStats,
      'skills_count': instance.skillsCount,
      'sessions': instance.sessions,
      'messages': instance.messages,
      'knowledge_graph': instance.knowledgeGraph,
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
      monthly: json['monthly'] as Map<String, dynamic>?,
      byProvider: json['by_provider'] as Map<String, dynamic>?,
      byModel: json['by_model'] as Map<String, dynamic>?,
      totalTokens: (json['total_tokens'] as num?)?.toInt(),
      totalCost: (json['total_cost'] as num?)?.toDouble(),
    );

Map<String, dynamic> _$$CostStatsImplToJson(_$CostStatsImpl instance) =>
    <String, dynamic>{
      'daily': instance.daily,
      'monthly': instance.monthly,
      'by_provider': instance.byProvider,
      'by_model': instance.byModel,
      'total_tokens': instance.totalTokens,
      'total_cost': instance.totalCost,
    };

_$AppConfigImpl _$$AppConfigImplFromJson(Map<String, dynamic> json) =>
    _$AppConfigImpl(
      config: json['config'] as Map<String, dynamic>?,
      timestamp: (json['timestamp'] as num?)?.toDouble(),
    );

Map<String, dynamic> _$$AppConfigImplToJson(_$AppConfigImpl instance) =>
    <String, dynamic>{
      'config': instance.config,
      'timestamp': instance.timestamp,
    };
