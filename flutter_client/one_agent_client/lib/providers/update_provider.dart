import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:open_filex/open_filex.dart';
import 'package:package_info_plus/package_info_plus.dart';

import '../api/update_api.dart';

/// 更新状态
class UpdateState {
  final bool isChecking;
  final bool isDownloading;
  final bool isResumed; // 当前下载是否为断点续传
  /// 问题4 修复：始终存储服务端最新 Release 信息（不做版本过滤）。
  /// 之前 getLatestRelease 在 currentVersion >= versionNumber 时返回 null，
  /// 导致 UI 无法显示"服务端最新版本是什么"。现在始终存储，由 hasUpdate
  /// 计算属性根据版本号比较决定是否提示更新。
  final ReleaseInfo? latestRelease;
  final int? currentVersion;
  final String? currentVersionName;
  final double downloadProgress; // 0.0 - 1.0
  final String? error;
  /// 问题7 修复：记录上次成功检查的时间戳。
  /// 之前检查成功但无新版本时，UI 没有任何反馈，用户以为"检查更新"按钮无效。
  /// 现在用此字段标记"已完成检查"，UI 据此显示"已是最新版本"。
  final DateTime? lastCheckedAt;

  const UpdateState({
    this.isChecking = false,
    this.isDownloading = false,
    this.isResumed = false,
    this.latestRelease,
    this.currentVersion,
    this.currentVersionName,
    this.downloadProgress = 0,
    this.error,
    this.lastCheckedAt,
  });

  /// 是否有新版本可用（问题4 修复：基于版本号比较，而非 latestRelease 是否为 null）
  bool get hasUpdate {
    final release = latestRelease;
    if (release == null || currentVersion == null) return false;
    return currentVersion! < release.versionNumber;
  }

  /// 服务端最新版本号（用于 UI 展示）
  int? get latestVersion => latestRelease?.versionNumber;

  /// 服务端最新版本标签（如 "app-v2074"）
  String? get latestTagName => latestRelease?.tagName;

  UpdateState copyWith({
    bool? isChecking,
    bool? isDownloading,
    bool? isResumed,
    ReleaseInfo? latestRelease,
    bool clearLatestRelease = false,
    int? currentVersion,
    String? currentVersionName,
    double? downloadProgress,
    bool clearError = false,
    String? error,
    DateTime? lastCheckedAt,
  }) => UpdateState(
    isChecking: isChecking ?? this.isChecking,
    isDownloading: isDownloading ?? this.isDownloading,
    isResumed: isResumed ?? this.isResumed,
    latestRelease: clearLatestRelease ? null : (latestRelease ?? this.latestRelease),
    currentVersion: currentVersion ?? this.currentVersion,
    currentVersionName: currentVersionName ?? this.currentVersionName,
    downloadProgress: downloadProgress ?? this.downloadProgress,
    // clearError=true 显式清空；未传 error 时保留旧值；传了 error 则覆盖
    error: clearError ? null : (error ?? this.error),
    lastCheckedAt: lastCheckedAt ?? this.lastCheckedAt,
  );
}

/// 应用更新 Provider
class UpdateNotifier extends StateNotifier<UpdateState> {
  UpdateNotifier() : super(const UpdateState());

  /// 检查更新
  Future<void> checkForUpdate() async {
    state = UpdateState(
      isChecking: true,
      currentVersion: state.currentVersion,
      currentVersionName: state.currentVersionName,
      error: null,
    );

    try {
      // 获取当前应用版本号
      final packageInfo = await PackageInfo.fromPlatform();
      final buildNumber = int.tryParse(packageInfo.buildNumber) ?? 0;
      final versionName = packageInfo.version;

      // 问题4 修复：使用 fetchLatestRelease 始终获取最新 Release 信息，
      // 不做版本号过滤。这样 UI 可以同时显示当前版本和服务端最新版本，
      // 用户也能在版本比较失误时手动强制下载。
      final release = await UpdateApi.fetchLatestRelease();

      if (release == null) {
        // 两个源都不可用 — 抛异常让 UI 显示"检测失败"
        state = UpdateState(
          isChecking: false,
          currentVersion: buildNumber,
          currentVersionName: versionName,
          error: '无法连接更新服务器（Gitee 和 GitHub 均不可达），请检查网络后重试',
          lastCheckedAt: DateTime.now(),
        );
        return;
      }

      // 问题7 修复：记录检查完成时间，无论是否有新版本，
      // UI 都能给出"已是最新版本"的反馈。
      // 问题4 修复：始终存储 latestRelease，由 hasUpdate 决定是否提示更新。
      state = UpdateState(
        isChecking: false,
        currentVersion: buildNumber,
        currentVersionName: versionName,
        latestRelease: release,
        lastCheckedAt: DateTime.now(),
      );

      // 检查成功后立即给出 SnackBar 反馈（在 UI 层由调用方处理）
    } catch (e) {
      state = UpdateState(
        isChecking: false,
        currentVersion: state.currentVersion,
        currentVersionName: state.currentVersionName,
        error: '检查更新失败: $e',
        lastCheckedAt: DateTime.now(),
      );
    }
  }

  /// 下载并安装更新
  Future<void> downloadAndInstall() async {
    final release = state.latestRelease;
    if (release == null) return;

    state = state.copyWith(
      isDownloading: true,
      downloadProgress: 0,
      isResumed: false,
      clearError: true,
    );

    try {
      final apkPath = await UpdateApi.downloadApkWithResume(
        release.apkUrl,
        giteeUrl: release.giteeApkUrl,
        onProgress: (received, total, isResumed) {
          if (total > 0) {
            state = state.copyWith(
              downloadProgress: received / total,
              isResumed: isResumed,
            );
          }
        },
      );

      // 调用系统安装器
      final result = await OpenFilex.open(apkPath);
      if (result.type != ResultType.done) {
        state = state.copyWith(
          isDownloading: false,
          isResumed: false,
          error: '无法打开安装器: ${result.message}',
        );
      } else {
        state = state.copyWith(
          isDownloading: false,
          isResumed: false,
          clearError: true,
        );
      }
    } catch (e) {
      state = state.copyWith(
        isDownloading: false,
        isResumed: false,
        error: '下载失败: $e',
      );
    }
  }

  /// 清除错误状态
  void clearError() {
    state = state.copyWith(clearError: true);
  }
}

final updateProvider = StateNotifierProvider<UpdateNotifier, UpdateState>(
  (ref) => UpdateNotifier(),
);
