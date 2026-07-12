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
    bool clearError = false,
  }) => ApprovalState(
    pending: pending ?? this.pending,
    isLoading: isLoading ?? this.isLoading,
    // 修复：用 clearError 显式控制清空
    error: clearError ? null : (error ?? this.error),
  );
}

class ApprovalNotifier extends StateNotifier<ApprovalState> {
  ApprovalNotifier() : super(const ApprovalState());

  Timer? _pollTimer;

  // 修复：竞态保护 + 错误退避 + 停止标志
  int _loadSeq = 0;
  int _consecutiveFailures = 0;
  bool _isPolling = false; // 修复：防止 stopPolling 后仍调度新 Timer
  static const int _baseIntervalSec = 3;
  static const int _maxIntervalSec = 60; // 错误退避上限

  Future<void> load() async {
    final requestId = ++_loadSeq;
    state = state.copyWith(isLoading: true, clearError: true);
    try {
      final pending = await ApprovalApi.listPending();
      if (requestId != _loadSeq) return; // 已有更新的请求
      _consecutiveFailures = 0; // 成功后重置失败计数
      state = state.copyWith(pending: pending, isLoading: false);
    } catch (e) {
      if (requestId != _loadSeq) return;
      _consecutiveFailures++;
      state = state.copyWith(error: e.toString(), isLoading: false);
    }
  }

  /// 开始轮询 — 修复：使用指数退避避免持续失败时浪费资源
  void startPolling() {
    _pollTimer?.cancel();
    _isPolling = true; // 修复：设置轮询标志
    _scheduleNextPoll();
  }

  /// 修复：递归调度轮询，失败时指数退避
  void _scheduleNextPoll() {
    // 修复：检查 _isPolling 标志，stopPolling 后不再调度新 Timer
    if (!_isPolling) return;
    // 失败时按 2^n 退避，但不超过 _maxIntervalSec
    final backoff = _consecutiveFailures > 0
        ? (_baseIntervalSec * (1 << (_consecutiveFailures.clamp(1, 6) - 1)))
            .clamp(_baseIntervalSec, _maxIntervalSec)
        : _baseIntervalSec;
    _pollTimer = Timer(Duration(seconds: backoff), () async {
      if (!_isPolling) return; // 修复：双重检查
      await load();
      _scheduleNextPoll();
    });
  }

  void stopPolling() {
    _isPolling = false; // 修复：清除轮询标志
    _pollTimer?.cancel();
    _pollTimer = null;
    _consecutiveFailures = 0; // 停止时重置
  }

  Future<bool> approve(String requestId) async {
    // 修复：失败时显式设置 error
    try {
      final ok = await ApprovalApi.approve(requestId);
      if (ok) {
        await load();
        state = state.copyWith(clearError: true);
      } else {
        state = state.copyWith(error: '审批失败');
      }
      return ok;
    } catch (e) {
      state = state.copyWith(error: '审批失败: $e');
      return false;
    }
  }

  Future<bool> deny(String requestId) async {
    try {
      final ok = await ApprovalApi.deny(requestId);
      if (ok) {
        await load();
        state = state.copyWith(clearError: true);
      } else {
        state = state.copyWith(error: '拒绝失败');
      }
      return ok;
    } catch (e) {
      state = state.copyWith(error: '拒绝失败: $e');
      return false;
    }
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
