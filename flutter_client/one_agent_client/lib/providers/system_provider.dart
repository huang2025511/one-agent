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
  }) => SystemState(
    stats: stats ?? this.stats,
    health: health ?? this.health,
    config: config ?? this.config,
    costs: costs ?? this.costs,
    isLoading: isLoading ?? this.isLoading,
    error: error,
  );
}

class SystemNotifier extends StateNotifier<SystemState> {
  SystemNotifier() : super(const SystemState());

  Future<void> loadAll() async {
    state = state.copyWith(isLoading: true, error: null);
    try {
      final stats = await SystemApi.getStats();
      final health = await SystemApi.getHealth();
      final config = await SystemApi.getConfig();
      final costs = await SystemApi.getCosts('daily');
      state = state.copyWith(
        stats: stats,
        health: health,
        config: config,
        costs: costs,
        isLoading: false,
      );
    } catch (e) {
      state = state.copyWith(error: e.toString(), isLoading: false);
    }
  }

  Future<bool> clearCache() async {
    return await SystemApi.clearCache();
  }
}

final systemProvider = StateNotifierProvider<SystemNotifier, SystemState>(
  (ref) => SystemNotifier(),
);
