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
  }) => MemoryState(
    memories: memories ?? this.memories,
    isLoading: isLoading ?? this.isLoading,
    error: error,
    searchQuery: searchQuery ?? this.searchQuery,
  );
}

class MemoryNotifier extends StateNotifier<MemoryState> {
  MemoryNotifier() : super(const MemoryState());

  Future<void> search(String query) async {
    if (query.trim().isEmpty) return;
    state = state.copyWith(isLoading: true, error: null, searchQuery: query);
    try {
      final results = await MemoryApi.search(query);
      state = state.copyWith(memories: results, isLoading: false);
    } catch (e) {
      state = state.copyWith(error: e.toString(), isLoading: false);
    }
  }

  Future<void> loadPage({int page = 1}) async {
    state = state.copyWith(isLoading: true, error: null);
    try {
      final pageData = await MemoryApi.getPage(page: page);
      state = state.copyWith(memories: pageData.items, isLoading: false);
    } catch (e) {
      state = state.copyWith(error: e.toString(), isLoading: false);
    }
  }

  Future<bool> add(String text, {String? tags}) async {
    final ok = await MemoryApi.add(text: text, tags: tags);
    if (ok) await loadPage();
    return ok;
  }
}

final memoryProvider = StateNotifierProvider<MemoryNotifier, MemoryState>(
  (ref) => MemoryNotifier(),
);
