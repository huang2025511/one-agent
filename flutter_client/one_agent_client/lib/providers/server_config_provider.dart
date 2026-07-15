import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/system_api.dart';

/// 服务端配置状态
class ServerConfigState {
  final Map<String, dynamic>? config;
  final bool isLoading;
  final String? error;
  final bool isSaving;

  const ServerConfigState({
    this.config,
    this.isLoading = false,
    this.error,
    this.isSaving = false,
  });

  ServerConfigState copyWith({
    Map<String, dynamic>? config,
    bool? isLoading,
    String? error,
    bool? isSaving,
    bool clearError = false,
  }) =>
      ServerConfigState(
        config: config ?? this.config,
        isLoading: isLoading ?? this.isLoading,
        // 修复：error ?? this.error 无法把 error 清空为 null（与其他 provider 一致
        // 用 clearError 显式控制）。否则 loadConfig/updateConfig 失败后 error
        // 永久残留在 UI 上，即使后续操作成功也无法清除。
        error: clearError ? null : (error ?? this.error),
        isSaving: isSaving ?? this.isSaving,
      );
}

/// 服务端配置 Provider
class ServerConfigNotifier extends StateNotifier<ServerConfigState> {
  ServerConfigNotifier() : super(const ServerConfigState());

  /// 加载配置
  Future<void> loadConfig() async {
    state = state.copyWith(isLoading: true, clearError: true);
    try {
      final result = await SystemApi.getConfig();
      if (result != null && result.config != null) {
        state = state.copyWith(
          config: result.config,
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

  /// 获取预算保护是否开启
  bool get costTrackingEnabled {
    final cfg = state.config;
    if (cfg == null) return false;
    final llm = cfg['llm'] as Map<String, dynamic>?;
    if (llm == null) return false;
    final cost = llm['cost_tracking'] as Map<String, dynamic>?;
    if (cost == null) return false;
    return cost['enabled'] == true;
  }

  /// 获取每日预算
  double get dailyBudget {
    final cfg = state.config;
    if (cfg == null) return 1.0;
    final llm = cfg['llm'] as Map<String, dynamic>?;
    if (llm == null) return 1.0;
    final cost = llm['cost_tracking'] as Map<String, dynamic>?;
    if (cost == null) return 1.0;
    return (cost['daily_budget'] as num?)?.toDouble() ?? 1.0;
  }

  /// 获取语言
  String get language {
    final cfg = state.config;
    if (cfg == null) return 'zh-CN';
    final agent = cfg['agent'] as Map<String, dynamic>?;
    if (agent == null) return 'zh-CN';
    return (agent['language'] as String?) ?? 'zh-CN';
  }

  /// 获取日志级别
  String get logLevel {
    final cfg = state.config;
    if (cfg == null) return 'INFO';
    final agent = cfg['agent'] as Map<String, dynamic>?;
    if (agent == null) return 'INFO';
    return (agent['log_level'] as String?) ?? 'INFO';
  }

  /// 获取本地 shell 是否启用
  bool get localShellEnabled {
    final cfg = state.config;
    if (cfg == null) return false;
    final exec = cfg['execution'] as Map<String, dynamic>?;
    if (exec == null) return false;
    final shell = exec['local_shell'] as Map<String, dynamic>?;
    if (shell == null) return false;
    return shell['enabled'] == true;
  }

  /// 获取自我进化是否启用
  bool get selfEvolutionEnabled {
    final cfg = state.config;
    if (cfg == null) return false;
    final router = cfg['router'] as Map<String, dynamic>?;
    if (router == null) return false;
    final se = router['self_evolution'] as Map<String, dynamic>?;
    if (se == null) return false;
    return se['enabled'] == true;
  }

  /// 获取上下文压缩是否启用
  bool get contextCompressionEnabled {
    final cfg = state.config;
    if (cfg == null) return false;
    final router = cfg['router'] as Map<String, dynamic>?;
    if (router == null) return false;
    final cc = router['context_compression'] as Map<String, dynamic>?;
    if (cc == null) return false;
    return cc['enabled'] == true;
  }
}

final serverConfigProvider =
    StateNotifierProvider<ServerConfigNotifier, ServerConfigState>(
  (ref) => ServerConfigNotifier(),
);
