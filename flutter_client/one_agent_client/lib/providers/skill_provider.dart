import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/skill_api.dart';
import '../models/skill.dart';

class SkillState {
  final List<Skill> skills;
  final List<MarketplacePackage> marketplace;
  final bool isLoading;
  final String? error;

  const SkillState({
    this.skills = const [],
    this.marketplace = const [],
    this.isLoading = false,
    this.error,
  });

  SkillState copyWith({
    List<Skill>? skills,
    List<MarketplacePackage>? marketplace,
    bool? isLoading,
    String? error,
  }) => SkillState(
    skills: skills ?? this.skills,
    marketplace: marketplace ?? this.marketplace,
    isLoading: isLoading ?? this.isLoading,
    error: error,
  );
}

class SkillNotifier extends StateNotifier<SkillState> {
  SkillNotifier() : super(const SkillState());

  Future<void> loadSkills() async {
    state = state.copyWith(isLoading: true, error: null);
    try {
      final skills = await SkillApi.listSkills();
      state = state.copyWith(skills: skills, isLoading: false);
    } catch (e) {
      state = state.copyWith(error: e.toString(), isLoading: false);
    }
  }

  Future<void> searchMarketplace(String query) async {
    state = state.copyWith(isLoading: true, error: null);
    try {
      final pkgs = await SkillApi.listMarketplace(query: query);
      state = state.copyWith(marketplace: pkgs, isLoading: false);
    } catch (e) {
      state = state.copyWith(error: e.toString(), isLoading: false);
    }
  }

  Future<bool> install(String name) async {
    final ok = await SkillApi.install(name);
    if (ok) await loadSkills();
    return ok;
  }
}

final skillProvider = StateNotifierProvider<SkillNotifier, SkillState>(
  (ref) => SkillNotifier(),
);
