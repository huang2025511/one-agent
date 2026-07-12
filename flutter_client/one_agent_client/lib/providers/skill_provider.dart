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
    bool clearError = false,
  }) => SkillState(
    skills: skills ?? this.skills,
    marketplace: marketplace ?? this.marketplace,
    isLoading: isLoading ?? this.isLoading,
    // 修复：用 clearError 显式控制清空
    error: clearError ? null : (error ?? this.error),
  );
}

class SkillNotifier extends StateNotifier<SkillState> {
  SkillNotifier() : super(const SkillState());

  // 修复：竞态保护序列号
  int _skillsSeq = 0;
  int _marketplaceSeq = 0;

  Future<void> loadSkills() async {
    final requestId = ++_skillsSeq;
    state = state.copyWith(isLoading: true, clearError: true);
    try {
      final skills = await SkillApi.listSkills();
      if (requestId != _skillsSeq) return;
      state = state.copyWith(skills: skills, isLoading: false);
    } catch (e) {
      if (requestId != _skillsSeq) return;
      state = state.copyWith(error: e.toString(), isLoading: false);
    }
  }

  Future<void> searchMarketplace(String query) async {
    // 修复：每个搜索请求独立序列号，避免快速输入的竞态条件
    final requestId = ++_marketplaceSeq;
    state = state.copyWith(isLoading: true, clearError: true);
    try {
      final pkgs = await SkillApi.listMarketplace(query: query);
      if (requestId != _marketplaceSeq) return; // 已有更新的搜索请求
      state = state.copyWith(marketplace: pkgs, isLoading: false);
    } catch (e) {
      if (requestId != _marketplaceSeq) return;
      state = state.copyWith(error: e.toString(), isLoading: false);
    }
  }

  Future<bool> install(String name) async {
    // 修复：install 失败时显式设置 error
    state = state.copyWith(isLoading: true, clearError: true);
    try {
      final ok = await SkillApi.install(name);
      if (ok) {
        await loadSkills();
        state = state.copyWith(clearError: true);
      } else {
        state = state.copyWith(error: '安装失败', isLoading: false);
      }
      return ok;
    } catch (e) {
      state = state.copyWith(error: '安装失败: $e', isLoading: false);
      return false;
    }
  }
}

final skillProvider = StateNotifierProvider<SkillNotifier, SkillState>(
  (ref) => SkillNotifier(),
);
