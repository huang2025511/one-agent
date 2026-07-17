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
        // 修复：用乐观更新——直接把新记忆插入列表头部，而非依赖 loadPage。
        // loadPage 可能被 _loadSeq 竞态丢弃（用户在 add 后立即触发新的 loadPage/search），
        // 导致刚添加的记忆不显示。乐观更新确保用户立即看到结果。
        final newMemory = Memory(
          // 临时 id，下次 loadPage/search 会用服务端真实数据替换
          id: DateTime.now().microsecondsSinceEpoch,
          text: text,
          source: 'mobile',
          tags: tags,
          createdAt: DateTime.now(),
        );
        state = state.copyWith(
          memories: [newMemory, ...state.memories],
          clearError: true,
          isLoading: false,
        );
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
