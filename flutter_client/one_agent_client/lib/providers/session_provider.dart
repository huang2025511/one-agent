import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/session_api.dart';
import '../models/session.dart';

/// 会话列表状态
class SessionListState {
  final List<Session> sessions;
  final bool isLoading;
  final String? error;

  const SessionListState({
    this.sessions = const [],
    this.isLoading = false,
    this.error,
  });

  SessionListState copyWith({
    List<Session>? sessions,
    bool? isLoading,
    String? error,
    bool clearError = false,
  }) => SessionListState(
    sessions: sessions ?? this.sessions,
    isLoading: isLoading ?? this.isLoading,
    // 修复：用 clearError 显式控制清空，避免 `error: error` 反模式
    // 导致其他状态更新时丢失 error 字段
    error: clearError ? null : (error ?? this.error),
  );
}

class SessionListNotifier extends StateNotifier<SessionListState> {
  SessionListNotifier() : super(const SessionListState());

  // 修复：将 _loadSeq 从全局变量改为实例字段，
  // 避免 autoDispose 或多实例时共享序列号导致竞态保护失效
  int _loadSeq = 0;

  Future<void> load() async {
    // 修复：清除竞态保护 — 记录请求序列号，回调时只接受最新请求
    final requestId = ++_loadSeq;
    state = state.copyWith(isLoading: true, clearError: true);
    try {
      final sessions = await SessionApi.listSessions();
      if (requestId != _loadSeq) return; // 已有更新的请求
      state = state.copyWith(sessions: sessions, isLoading: false);
    } catch (e) {
      if (requestId != _loadSeq) return; // 已有更新的请求
      state = state.copyWith(error: e.toString(), isLoading: false);
    }
  }

  Future<bool> delete(String sessionId) async {
    try {
      final ok = await SessionApi.deleteSession(sessionId);
      if (ok) {
        state = state.copyWith(
          sessions: state.sessions.where((s) => s.id != sessionId).toList(),
          clearError: true,
        );
      } else {
        state = state.copyWith(error: '删除会话失败');
      }
      return ok;
    } catch (e) {
      state = state.copyWith(error: '删除会话失败: $e');
      return false;
    }
  }

  Future<String?> fork(String sessionId) async {
    try {
      final newId = await SessionApi.forkSession(sessionId);
      if (newId == null) {
        state = state.copyWith(error: '分叉会话失败');
      } else {
        state = state.copyWith(clearError: true);
      }
      return newId;
    } catch (e) {
      state = state.copyWith(error: '分叉会话失败: $e');
      return null;
    }
  }
}

final sessionListProvider = StateNotifierProvider<SessionListNotifier, SessionListState>(
  (ref) => SessionListNotifier(),
);
