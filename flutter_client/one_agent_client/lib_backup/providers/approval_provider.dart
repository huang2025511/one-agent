import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/approval_api.dart';
import '../models/approval.dart';

class ApprovalState {
  final List<ApprovalRequest> pending;
  final bool isLoading;
  final String? error;

  const ApprovalState({
    this.pending = const [],
    this.isLoading = false,
    this.error,
  });

  ApprovalState copyWith({
    List<ApprovalRequest>? pending,
    bool? isLoading,
    String? error,
  }) => ApprovalState(
    pending: pending ?? this.pending,
    isLoading: isLoading ?? this.isLoading,
    error: error,
  );
}

class ApprovalNotifier extends StateNotifier<ApprovalState> {
  ApprovalNotifier() : super(const ApprovalState());

  Timer? _pollTimer;

  Future<void> load() async {
    state = state.copyWith(isLoading: true, error: null);
    try {
      final pending = await ApprovalApi.listPending();
      state = state.copyWith(pending: pending, isLoading: false);
    } catch (e) {
      state = state.copyWith(error: e.toString(), isLoading: false);
    }
  }

  /// 开始轮询（每 3 秒检查一次新审批）
  void startPolling() {
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(const Duration(seconds: 3), (_) => load());
  }

  void stopPolling() {
    _pollTimer?.cancel();
    _pollTimer = null;
  }

  Future<bool> approve(String requestId) async {
    final ok = await ApprovalApi.approve(requestId);
    if (ok) await load();
    return ok;
  }

  Future<bool> deny(String requestId) async {
    final ok = await ApprovalApi.deny(requestId);
    if (ok) await load();
    return ok;
  }

  @override
  void dispose() {
    _pollTimer?.cancel();
    super.dispose();
  }
}

final approvalProvider = StateNotifierProvider<ApprovalNotifier, ApprovalState>(
  (ref) => ApprovalNotifier(),
);
