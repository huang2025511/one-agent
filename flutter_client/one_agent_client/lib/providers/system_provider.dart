import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/system_api.dart';
import '../models/system_stats.dart';

class SystemState {
  final SystemStats? stats;
  final SystemHealth? health;
  final AppConfig? config;
  final CostStats? costs;
  final bool isLoading;
  final String? error;

  const SystemState({
    this.stats,
    this.health,
    this.config,
    this.costs,
    this.isLoading = false,
    this.error,
  });

  SystemState copyWith({
    SystemStats? stats,
    SystemHealth? health,
    AppConfig? config,
    CostStats? costs,
    bool? isLoading,
    String? error,
    bool clearError = false,
  }) => SystemState(
    stats: stats ?? this.stats,
    health: health ?? this.health,
    config: config ?? this.config,
    costs: costs ?? this.costs,
    isLoading: isLoading ?? this.isLoading,
    // 修复：用 clearError 显式控制清空
    error: clearError ? null : (error ?? this.error),
  );
}

class SystemNotifier extends StateNotifier<SystemState> {
  SystemNotifier() : super(const SystemState());

  Future<void> loadAll() async {
    state = state.copyWith(isLoading: true, clearError: true);
    // 修复：用 Future.wait 并行请求 4 个 API（之前是串行，浪费时间）
    // 修复：用 try/catch 单独包裹每个请求，容忍部分失败
    // 只要 stats 成功就展示，其他失败也不影响 stats 显示
    final results = await Future.wait<dynamic>([
      _safeCall(SystemApi.getStats),
      _safeCall(SystemApi.getHealth),
      _safeCall(SystemApi.getConfig),
      _safeCall(() => SystemApi.getCosts('daily')),
    ]);

    final stats = results[0] as SystemStats?;
    final health = results[1] as SystemHealth?;
    final config = results[2] as AppConfig?;
    final costs = results[3] as CostStats?;

    // 收集所有失败原因
    final errors = <String>[];
    if (stats == null) errors.add('stats');
    if (health == null) errors.add('health');
    if (config == null) errors.add('config');
    if (costs == null) errors.add('costs');

    state = state.copyWith(
      stats: stats,
      health: health,
      config: config,
      costs: costs,
      isLoading: false,
      // 修复：只有全部失败时才显示错误信息；部分失败时只在 error 字段标注
      error: errors.length == 4 ? '所有系统信息加载失败' : (errors.isNotEmpty ? '部分加载失败: ${errors.join(", ")}' : null),
      clearError: errors.isEmpty,
    );
  }

  /// 修复：安全调用，捕获异常并返回 null（不抛出）
  Future<T?> _safeCall<T>(Future<T> Function() fn) async {
    try {
      return await fn();
    } catch (e) {
      // 单个 API 失败不影响其他 API 的加载
      return null;
    }
  }

  Future<bool> clearCache() async {
    return await SystemApi.clearCache();
  }
}

final systemProvider = StateNotifierProvider<SystemNotifier, SystemState>(
  (ref) => SystemNotifier(),
);
