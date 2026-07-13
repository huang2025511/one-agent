import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/role_api.dart';
import '../models/role.dart';

class RoleState {
  final List<Role> roles;
  final bool isLoading;
  final String? error;

  const RoleState({
    this.roles = const [],
    this.isLoading = false,
    this.error,
  });

  RoleState copyWith({
    List<Role>? roles,
    bool? isLoading,
    String? error,
    bool clearError = false,
  }) => RoleState(
    roles: roles ?? this.roles,
    isLoading: isLoading ?? this.isLoading,
    error: clearError ? null : (error ?? this.error),
  );
}

class RoleNotifier extends StateNotifier<RoleState> {
  RoleNotifier() : super(const RoleState());

  Future<void> load() async {
    state = state.copyWith(isLoading: true, clearError: true);
    try {
      final roles = await RoleApi.list();
      state = state.copyWith(roles: roles, isLoading: false);
    } catch (e) {
      state = state.copyWith(error: e.toString(), isLoading: false);
    }
  }

  Future<bool> create({
    required String name,
    String description = '',
    String systemPromptOverride = '',
    String icon = '🤖',
    String color = '#6750A4',
  }) async {
    state = state.copyWith(isLoading: true, clearError: true);
    try {
      await RoleApi.create(
        name: name,
        description: description,
        systemPromptOverride: systemPromptOverride,
        icon: icon,
        color: color,
      );
      await load();
      return true;
    } catch (e) {
      state = state.copyWith(error: e.toString(), isLoading: false);
      return false;
    }
  }

  Future<bool> update(int id, {
    String? name,
    String? description,
    String? systemPromptOverride,
    String? icon,
    String? color,
  }) async {
    state = state.copyWith(isLoading: true, clearError: true);
    try {
      await RoleApi.update(
        id,
        name: name,
        description: description,
        systemPromptOverride: systemPromptOverride,
        icon: icon,
        color: color,
      );
      await load();
      return true;
    } catch (e) {
      state = state.copyWith(error: e.toString(), isLoading: false);
      return false;
    }
  }

  Future<bool> delete(int id) async {
    state = state.copyWith(isLoading: true, clearError: true);
    try {
      await RoleApi.delete(id);
      await load();
      return true;
    } catch (e) {
      state = state.copyWith(error: e.toString(), isLoading: false);
      return false;
    }
  }

  Future<bool> activate(int id) async {
    state = state.copyWith(isLoading: true, clearError: true);
    try {
      await RoleApi.activate(id);
      await load();
      return true;
    } catch (e) {
      state = state.copyWith(error: e.toString(), isLoading: false);
      return false;
    }
  }

  Future<bool> deactivate() async {
    state = state.copyWith(isLoading: true, clearError: true);
    try {
      await RoleApi.deactivate();
      await load();
      return true;
    } catch (e) {
      state = state.copyWith(error: e.toString(), isLoading: false);
      return false;
    }
  }
}

final roleProvider = StateNotifierProvider<RoleNotifier, RoleState>(
  (ref) => RoleNotifier(),
);
