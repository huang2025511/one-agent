import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:open_filex/open_filex.dart';
import 'package:package_info_plus/package_info_plus.dart';

import '../api/update_api.dart';

/// 更新状态
class UpdateState {
  final bool isChecking;
  final bool isDownloading;
  final ReleaseInfo? latestRelease;
  final int? currentVersion;
  final String? currentVersionName;
  final double downloadProgress; // 0.0 - 1.0
  final String? error;

  const UpdateState({
    this.isChecking = false,
    this.isDownloading = false,
    this.latestRelease,
    this.currentVersion,
    this.currentVersionName,
    this.downloadProgress = 0,
    this.error,
  });

  /// 是否有新版本可用
  bool get hasUpdate => latestRelease != null;

  UpdateState copyWith({
    bool? isChecking,
    bool? isDownloading,
    ReleaseInfo? latestRelease,
    int? currentVersion,
    String? currentVersionName,
    double? downloadProgress,
    String? error,
  }) => UpdateState(
    isChecking: isChecking ?? this.isChecking,
    isDownloading: isDownloading ?? this.isDownloading,
    latestRelease: latestRelease ?? this.latestRelease,
    currentVersion: currentVersion ?? this.currentVersion,
    currentVersionName: currentVersionName ?? this.currentVersionName,
    downloadProgress: downloadProgress ?? this.downloadProgress,
    error: error,
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

      final release = await UpdateApi.getLatestRelease(
        currentVersion: buildNumber,
      );

      state = UpdateState(
        isChecking: false,
        currentVersion: buildNumber,
        currentVersionName: versionName,
        latestRelease: release,
      );
    } catch (e) {
      state = UpdateState(
        isChecking: false,
        currentVersion: state.currentVersion,
        currentVersionName: state.currentVersionName,
        error: '检查更新失败: $e',
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
      error: null,
    );

    try {
      final apkPath = await UpdateApi.downloadApk(
        release.apkUrl,
        giteeUrl: release.giteeApkUrl,
        onProgress: (received, total) {
          if (total > 0) {
            state = state.copyWith(
              downloadProgress: received / total,
            );
          }
        },
      );

      // 调用系统安装器
      final result = await OpenFilex.open(apkPath);
      if (result.type != ResultType.done) {
        state = state.copyWith(
          isDownloading: false,
          error: '无法打开安装器: ${result.message}',
        );
      } else {
        state = state.copyWith(isDownloading: false);
      }
    } catch (e) {
      state = state.copyWith(
        isDownloading: false,
        error: '下载失败: $e',
      );
    }
  }

  /// 清除错误状态
  void clearError() {
    state = state.copyWith(error: null);
  }
}

final updateProvider = StateNotifierProvider<UpdateNotifier, UpdateState>(
  (ref) => UpdateNotifier(),
);
