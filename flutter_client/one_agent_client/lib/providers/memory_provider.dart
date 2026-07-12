import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/memory_api.dart';
import '../models/memory.dart';

class MemoryState {
  final List<Memory> memories;
  final bool isLoading;
  final String? error;
  final String searchQuery;

  const MemoryState({
    this.memories = const [],
    this.isLoading = false,
    this.error,
    this.searchQuery = '',
  });

  MemoryState copyWith({
    List<Memory>? memories,
    bool? isLoading,
    String? error,
    String? searchQuery,
    bool clearError = false,
  }) => MemoryState(
    memories: memories ?? this.memories,
    isLoading: isLoading ?? this.isLoading,
    // 修复：用 clearError 显式控制清空
    error: clearError ? null : (error ?? this.error),
    searchQuery: searchQuery ?? this.searchQuery,
  );
}

class MemoryNotifier extends StateNotifier<MemoryState> {
  MemoryNotifier() : super(const MemoryState());

  // 修复：竞态保护序列号
  int _searchSeq = 0;
  int _loadSeq = 0;

  Future<void> search(String query) async {
    if (query.trim().isEmpty) {
      // 修复：清空查询时也要重置 isLoading
      state = state.copyWith(isLoading: false, clearError: true);
      return;
    }
    final requestId = ++_searchSeq;
    state = state.copyWith(isLoading: true, clearError: true, searchQuery: query);
    try {
      final results = await MemoryApi.search(query);
      if (requestId != _searchSeq) return; // 已有更新的搜索请求
      state = state.copyWith(memories: results, isLoading: false);
    } catch (e) {
      if (requestId != _searchSeq) return;
      state = state.copyWith(error: e.toString(), isLoading: false);
    }
  }

  Future<void> loadPage({int page = 1}) async {
    final requestId = ++_loadSeq;
    state = state.copyWith(isLoading: true, clearError: true);
    try {
      final pageData = await MemoryApi.getPage(page: page);
      if (requestId != _loadSeq) return;
      state = state.copyWith(memories: pageData.items, isLoading: false);
    } catch (e) {
      if (requestId != _loadSeq) return;
      state = state.copyWith(error: e.toString(), isLoading: false);
    }
  }

  Future<bool> add(String text, {String? tags}) async {
    // 修复：add 失败时显式设置 error
    state = state.copyWith(isLoading: true, clearError: true);
    try {
      final ok = await MemoryApi.add(text: text, tags: tags);
      if (ok) {
        await loadPage();
        state = state.copyWith(clearError: true);
      } else {
        state = state.copyWith(error: '添加失败', isLoading: false);
      }
      return ok;
    } catch (e) {
      state = state.copyWith(error: '添加失败: $e', isLoading: false);
      return false;
    }
  }
}

final memoryProvider = StateNotifierProvider<MemoryNotifier, MemoryState>(
  (ref) => MemoryNotifier(),
);
