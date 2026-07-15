import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/system_api.dart';
import '../models/system_stats.dart';

/// 服务端配置状态
class ServerConfigState {
  final Map<String, dynamic>? config;
  /// 模型目录数据（来自 /api/models）
  final Map<String, dynamic>? models;
  final bool isLoading;
  final String? error;
  final bool isSaving;

  const ServerConfigState({
    this.config,
    this.models,
    this.isLoading = false,
    this.error,
    this.isSaving = false,
  });

  ServerConfigState copyWith({
    Map<String, dynamic>? config,
    Map<String, dynamic>? models,
    bool? isLoading,
    String? error,
    bool? isSaving,
    bool clearError = false,
  }) =>
      ServerConfigState(
        config: config ?? this.config,
        models: models ?? this.models,
        isLoading: isLoading ?? this.isLoading,
        error: clearError ? null : (error ?? this.error),
        isSaving: isSaving ?? this.isSaving,
      );
}

/// 服务端配置 Provider — 统一管理所有 one-agent 设置
class ServerConfigNotifier extends StateNotifier<ServerConfigState> {
  ServerConfigNotifier() : super(const ServerConfigState());

  /// 加载配置 + 模型目录
  Future<void> loadConfig() async {
    state = state.copyWith(isLoading: true, clearError: true);
    try {
      final results = await Future.wait([
        SystemApi.getConfig(),
        SystemApi.getModels(),
      ]);
      final configResult = results[0] as AppConfig?;
      final modelsResult = results[1] as Map<String, dynamic>?;
      if (configResult != null && configResult.config != null) {
        state = state.copyWith(
          config: configResult.config,
          models: modelsResult,
          isLoading: false,
          clearError: true,
        );
      } else {
        state = state.copyWith(
          isLoading: false,
          error: '加载配置失败',
        );
      }
    } catch (e) {
      state = state.copyWith(
        isLoading: false,
        error: e.toString(),
      );
    }
  }

  /// 仅刷新模型目录
  Future<void> loadModels() async {
    try {
      final models = await SystemApi.getModels();
      if (models != null) {
        state = state.copyWith(models: models);
      }
    } catch (_) {}
  }

  /// 更新配置
  Future<bool> updateConfig(Map<String, dynamic> updates) async {
    state = state.copyWith(isSaving: true, clearError: true);
    try {
      final result = await SystemApi.updateConfig(updates);
      if (result != null && result['status'] == 'ok') {
        state = state.copyWith(
          config: result['config'] as Map<String, dynamic>,
          isSaving: false,
          clearError: true,
        );
        return true;
      }
      state = state.copyWith(
        isSaving: false,
        error: result?['message'] ?? '保存失败',
      );
      return false;
    } catch (e) {
      state = state.copyWith(
        isSaving: false,
        error: e.toString(),
      );
      return false;
    }
  }

  // ════════════════════════════════════════════════════════════
  //  通用嵌套 Map 取值辅助
  // ════════════════════════════════════════════════════════════
  dynamic _get(List<String> path, [dynamic fallback]) {
    dynamic cur = state.config;
    for (final key in path) {
      if (cur is Map<String, dynamic>) {
        cur = cur[key];
      } else {
        return fallback;
      }
    }
    return cur ?? fallback;
  }

  bool _getBool(List<String> path, [bool fallback = false]) {
    return _get(path, fallback) == true;
  }

  String _getString(List<String> path, [String fallback = '']) {
    final v = _get(path, fallback);
    return v?.toString() ?? fallback;
  }

  double _getDouble(List<String> path, [double fallback = 0]) {
    final v = _get(path);
    return (v as num?)?.toDouble() ?? fallback;
  }

  int _getInt(List<String> path, [int fallback = 0]) {
    final v = _get(path);
    return (v as num?)?.toInt() ?? fallback;
  }

  // ════════════════════════════════════════════════════════════
  //  Agent 配置
  // ════════════════════════════════════════════════════════════
  String get agentName => _getString(['agent', 'name'], 'One-Agent');
  String get language => _getString(['agent', 'language'], 'zh-CN');
  String get logLevel => _getString(['agent', 'log_level'], 'INFO').toUpperCase();
  String get timezone => _getString(['agent', 'timezone'], 'UTC');
  String get dataDir => _getString(['agent', 'data_dir'], './data');
  String get agentVersion => _getString(['agent', 'version'], '2.0.0');

  // ════════════════════════════════════════════════════════════
  //  LLM 模型配置
  // ════════════════════════════════════════════════════════════
  String get primaryModel => _getString(['llm', 'primary_model'], '');
  String get primaryProvider => _getString(['llm', 'primary_provider'], '');
  String get lightweightModel => _getString(['llm', 'lightweight_model'], '');
  double get defaultTemperature => _getDouble(['llm', 'default_temperature'], 0.3);
  int get defaultMaxTokens => _getInt(['llm', 'default_max_tokens'], 2048);
  int get llmTimeout => _getInt(['llm', 'timeout'], 60);
  int get llmRetries => _getInt(['llm', 'retries'], 3);
  bool get semanticCacheEnabled => _getBool(['llm', 'semantic_cache', 'enabled'], true);
  double get semanticCacheThreshold => _getDouble(['llm', 'semantic_cache', 'threshold'], 0.92);

  // ════════════════════════════════════════════════════════════
  //  成本追踪
  // ════════════════════════════════════════════════════════════
  bool get costTrackingEnabled => _getBool(['llm', 'cost_tracking', 'enabled'], false);
  double get dailyBudget => _getDouble(['llm', 'cost_tracking', 'daily_budget'], 1.0);
  double get monthlyBudget => _getDouble(['llm', 'cost_tracking', 'monthly_budget'], 20.0);

  // ════════════════════════════════════════════════════════════
  //  路由配置
  // ════════════════════════════════════════════════════════════
  bool get routingEnabled => _getBool(['router', 'enabled'], true);
  bool get selfEvolutionEnabled => _getBool(['router', 'self_evolution', 'enabled'], false);
  bool get contextCompressionEnabled => _getBool(['router', 'context_compression', 'enabled'], false);
  bool get skillLazyLoadingEnabled => _getBool(['router', 'skill_lazy_loading', 'enabled'], true);
  double get thresholdTrivial => _getDouble(['router', 'task_complexity_thresholds', 'trivial'], 0.2);
  double get thresholdSimple => _getDouble(['router', 'task_complexity_thresholds', 'simple'], 0.5);
  double get thresholdComplex => _getDouble(['router', 'task_complexity_thresholds', 'complex'], 0.8);

  // ════════════════════════════════════════════════════════════
  //  记忆配置
  // ════════════════════════════════════════════════════════════
  int get memoryMaxTurns => _getInt(['memory', 'short_term', 'max_turns'], 20);
  int get memoryMaxTokens => _getInt(['memory', 'short_term', 'max_tokens'], 8000);
  bool get longTermMemoryEnabled => _getBool(['memory', 'long_term', 'enabled'], true);
  int get longTermMaxResults => _getInt(['memory', 'long_term', 'max_results'], 5);
  bool get memoryDecayEnabled => _getBool(['memory', 'long_term', 'decay_enabled'], true);
  bool get proceduralMemoryEnabled => _getBool(['memory', 'procedural', 'enabled'], true);
  bool get autoCreateSkills => _getBool(['memory', 'procedural', 'auto_create_skills'], true);

  // ════════════════════════════════════════════════════════════
  //  执行环境
  // ════════════════════════════════════════════════════════════
  bool get localShellEnabled => _getBool(['execution', 'local_shell', 'enabled'], false);
  bool get dockerEnabled => _getBool(['execution', 'docker', 'enabled'], false);
  String get dockerImage => _getString(['execution', 'docker', 'image'], 'python:3.11-slim');
  bool get browserEnabled => _getBool(['execution', 'browser', 'enabled'], false);

  // ════════════════════════════════════════════════════════════
  //  安全配置
  // ════════════════════════════════════════════════════════════
  bool get systemExecutorEnabled => _getBool(['security', 'system_executor_enabled'], true);
  bool get requirePasswordForDangerous => _getBool(['security', 'require_password_for_dangerous'], true);
  int get commandTimeoutSeconds => _getInt(['security', 'command_timeout_seconds'], 30);
  int get maxPasswordAttempts => _getInt(['security', 'max_password_attempts'], 3);

  // ════════════════════════════════════════════════════════════
  //  REST API 配置
  // ════════════════════════════════════════════════════════════
  String get restHost => _getString(['rest', 'host'], '127.0.0.1');
  int get restPort => _getInt(['rest', 'port'], 18792);
  int get rateLimitPerMinute => _getInt(['rest', 'rate_limit_per_minute'], 60);

  // ════════════════════════════════════════════════════════════
  //  监控配置
  // ════════════════════════════════════════════════════════════
  bool get monitoringEnabled => _getBool(['monitoring', 'enabled'], true);
  int get monitoringPort => _getInt(['monitoring', 'port'], 18793);

  // ════════════════════════════════════════════════════════════
  //  LLM 缓存
  // ════════════════════════════════════════════════════════════
  bool get llmCacheEnabled => _getBool(['llm_cache', 'enabled'], true);
  int get llmCacheTtl => _getInt(['llm_cache', 'ttl_seconds'], 3600);
  int get llmCacheMaxSize => _getInt(['llm_cache', 'max_size'], 500);

  // ════════════════════════════════════════════════════════════
  //  模型目录数据（来自 /api/models）
  // ════════════════════════════════════════════════════════════
  Map<String, dynamic>? get modelsData => state.models;
  String get catalogDefaultModel =>
      (state.models?['default_model'] as String?) ?? primaryModel;
  String get catalogPrimaryProvider =>
      (state.models?['primary_provider'] as String?) ?? primaryProvider;
  bool get catalogRoutingEnabled =>
      (state.models?['routing_enabled'] as bool?) ?? routingEnabled;
  Map<String, dynamic>? get tierData =>
      state.models?['tiers'] as Map<String, dynamic>?;
  Map<String, dynamic>? get modelsByCategory =>
      state.models?['models_by_category'] as Map<String, dynamic>?;
  List<dynamic>? get availableModels =>
      state.models?['available_models'] as List<dynamic>?;

  /// 已配置 API Key 的服务商列表（来自 config.llm.api_keys，值为 "***" 表示已配置）
  List<String> get configuredProviders {
    final keys = _get(['llm', 'api_keys']) as Map<String, dynamic>?;
    if (keys == null) return [];
    return keys.entries
        .where((e) {
          final v = e.value;
          // v == "***"（脱敏）或非空字符串表示已配置
          return v is String && v.isNotEmpty;
        })
        .map((e) => e.key)
        .toList()
      ..sort();
  }
}

final serverConfigProvider =
    StateNotifierProvider<ServerConfigNotifier, ServerConfigState>(
  (ref) => ServerConfigNotifier(),
);
