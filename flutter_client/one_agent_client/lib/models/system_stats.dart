import 'package:freezed_annotation/freezed_annotation.dart';

part 'system_stats.freezed.dart';
part 'system_stats.g.dart';

/// 系统统计
@freezed
class SystemStats with _$SystemStats {
  const factory SystemStats({
    int? uptimeSeconds,
    Map<String, dynamic>? busMetrics,
    Map<String, dynamic>? llmStats,
    Map<String, dynamic>? memoryStats,
    int? skillsCount,
    Map<String, dynamic>? sessions,
    Map<String, dynamic>? messages,
    Map<String, dynamic>? knowledgeGraph,
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
    Map<String, dynamic>? byProvider,
    Map<String, dynamic>? byModel,
    int? totalTokens,
    double? totalCost,
  }) = _CostStats;

  factory CostStats.fromJson(Map<String, dynamic> json) =>
      _$CostStatsFromJson(json);
}

/// 应用配置
@freezed
class AppConfig with _$AppConfig {
  const factory AppConfig({
    Map<String, dynamic>? config,
    DateTime? timestamp,
  }) = _AppConfig;

  factory AppConfig.fromJson(Map<String, dynamic> json) =>
      _$AppConfigFromJson(json);
}
