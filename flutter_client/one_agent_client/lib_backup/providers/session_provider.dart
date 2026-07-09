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
  }) => SessionListState(
    sessions: sessions ?? this.sessions,
    isLoading: isLoading ?? this.isLoading,
    error: error,
  );
}

class SessionListNotifier extends StateNotifier<SessionListState> {
  SessionListNotifier() : super(const SessionListState());

  Future<void> load() async {
    state = state.copyWith(isLoading: true, error: null);
    try {
      final sessions = await SessionApi.listSessions();
      state = state.copyWith(sessions: sessions, isLoading: false);
    } catch (e) {
      state = state.copyWith(error: e.toString(), isLoading: false);
    }
  }

  Future<bool> delete(String sessionId) async {
    final ok = await SessionApi.deleteSession(sessionId);
    if (ok) {
      state = state.copyWith(
        sessions: state.sessions.where((s) => s.id != sessionId).toList(),
      );
    }
    return ok;
  }

  Future<String?> fork(String sessionId) async {
    return await SessionApi.forkSession(sessionId);
  }
}

final sessionListProvider = StateNotifierProvider<SessionListNotifier, SessionListState>(
  (ref) => SessionListNotifier(),
);
