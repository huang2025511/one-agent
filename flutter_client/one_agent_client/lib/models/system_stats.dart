import 'package:freezed_annotation/freezed_annotation.dart';

part 'system_stats.freezed.dart';
part 'system_stats.g.dart';

/// 系统统计
@freezed
class SystemStats with _$SystemStats {
  const factory SystemStats({
    @JsonKey(name: 'uptime_seconds') int? uptimeSeconds,
    @JsonKey(name: 'bus_metrics') Map<String, dynamic>? busMetrics,
    @JsonKey(name: 'llm_stats') Map<String, dynamic>? llmStats,
    @JsonKey(name: 'memory_stats') Map<String, dynamic>? memoryStats,
    @JsonKey(name: 'skills_count') int? skillsCount,
    Map<String, dynamic>? sessions,
    Map<String, dynamic>? messages,
    @JsonKey(name: 'knowledge_graph') Map<String, dynamic>? knowledgeGraph,
  }) = _SystemStats;

  factory SystemStats.fromJson(Map<String, dynamic> json) =>
      _$SystemStatsFromJson(json);
}

/// 系统健康
@freezed
class SystemHealth with _$SystemHealth {
  const factory SystemHealth({
    required String status,
    int? uptime,
    String? version,
    Map<String, dynamic>? components,
  }) = _SystemHealth;

  factory SystemHealth.fromJson(Map<String, dynamic> json) =>
      _$SystemHealthFromJson(json);
}

/// 成本统计
@freezed
class CostStats with _$CostStats {
  const factory CostStats({
    Map<String, dynamic>? daily,
    Map<String, dynamic>? monthly,
    @JsonKey(name: 'by_provider') Map<String, dynamic>? byProvider,
    @JsonKey(name: 'by_model') Map<String, dynamic>? byModel,
    @JsonKey(name: 'total_tokens') int? totalTokens,
    @JsonKey(name: 'total_cost') double? totalCost,
  }) = _CostStats;

  factory CostStats.fromJson(Map<String, dynamic> json) =>
      _$CostStatsFromJson(json);
}

/// 应用配置
@freezed
class AppConfig with _$AppConfig {
  const factory AppConfig({
    Map<String, dynamic>? config,
    /// 服务端返回 float epoch（time.time()），不是 ISO 字符串。
    /// 用 double? 原样保存，避免 DateTime.parse 抛异常。
    double? timestamp,
  }) = _AppConfig;

  factory AppConfig.fromJson(Map<String, dynamic> json) =>
      _$AppConfigFromJson(json);
}
