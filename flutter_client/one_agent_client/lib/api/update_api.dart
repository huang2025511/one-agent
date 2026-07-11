import 'package:dio/dio.dart';
import 'package:path_provider/path_provider.dart';
import 'dart:io';

/// Release 信息（同时持有 GitHub 和 Gitee 下载地址，下载时优先 Gitee）
class ReleaseInfo {
  final String tagName;      // e.g. "app-v24"
  final String name;         // e.g. "One-Agent App v24"
  final String? body;        // Release notes
  final String? publishedAt; // ISO8601
  final String apkUrl;       // GitHub 下载地址（国外）
  final String? giteeApkUrl; // Gitee 下载地址（国内加速）
  final int apkSize;         // APK 字节数

  const ReleaseInfo({
    required this.tagName,
    required this.name,
    this.body,
    this.publishedAt,
    required this.apkUrl,
    this.giteeApkUrl,
    required this.apkSize,
  });

  /// 从 tag_name 解析数字版本号（如 "app-v24" → 24）
  int get versionNumber {
    final m = RegExp(r'(\d+)\s*$').firstMatch(tagName);
    return m != null ? int.parse(m.group(1)!) : 0;
  }
}

/// 应用更新 API — 同时查询 GitHub 和 Gitee，下载时优先 Gitee（国内加速）
class UpdateApi {
  static const String _ghRepo = 'huang2025511/one-agent';
  static const String _giteeRepo = 'huang20260511/one-agent';

  /// 获取最新 Release
  /// [currentVersion] 当前应用版本号（用于无新版本时返回 null）
  static Future<ReleaseInfo?> getLatestRelease({int? currentVersion}) async {
    final dio = Dio(BaseOptions(
      connectTimeout: const Duration(seconds: 15),
      receiveTimeout: const Duration(seconds: 15),
      headers: {'Accept': 'application/vnd.github+json'},
    ));

    try {
      // 1. 从 GitHub 获取 Release 元信息（GitHub API 稳定，作为权威源）
      final resp = await dio.get(
        'https://api.github.com/repos/$_ghRepo/releases/latest',
      );
      final data = resp.data as Map<String, dynamic>;
      final tagName = data['tag_name'] as String? ?? '';
      final name = data['name'] as String? ?? tagName;
      final body = data['body'] as String?;
      final publishedAt = data['published_at'] as String?;

      String? apkUrl;
      int apkSize = 0;
      for (final asset in (data['assets'] as List<dynamic>? ?? [])) {
        final assetName = asset['name'] as String? ?? '';
        if (assetName.endsWith('.apk')) {
          apkUrl = asset['browser_download_url'] as String?;
          apkSize = (asset['size'] as num?)?.toInt() ?? 0;
          break;
        }
      }

      if (apkUrl == null) return null;

      // 2. 构造 Gitee 下载地址（同名 tag，附件名固定为 app-release.apk）
      //    Gitee Release 附件 URL 格式：
      //    https://gitee.com/{owner}/{repo}/releases/download/{tag}/{asset_name}
      final giteeApkUrl =
          'https://gitee.com/$_giteeRepo/releases/download/$tagName/app-release.apk';

      final release = ReleaseInfo(
        tagName: tagName,
        name: name,
        body: body,
        publishedAt: publishedAt,
        apkUrl: apkUrl,
        giteeApkUrl: giteeApkUrl,
        apkSize: apkSize,
      );

      // 比较版本号：若当前版本 >= 最新版本，则无需更新
      if (currentVersion != null &&
          currentVersion >= release.versionNumber) {
        return null;
      }
      return release;
    } on DioException catch (e) {
      if (e.response?.statusCode == 404) return null;
      rethrow;
    } finally {
      dio.close();
    }
  }

  /// 下载 APK 到临时目录，返回本地文件路径
  /// 优先从 Gitee 下载（国内快），失败回退 GitHub
  /// [onProgress] 接收 (received, total) 用于显示进度
  static Future<String> downloadApk(
    String url, {
    String? giteeUrl,
    void Function(int received, int total)? onProgress,
  }) async {
    final tempDir = await getTemporaryDirectory();
    final savePath = '${tempDir.path}/one-agent-update.apk';

    // 如果已存在旧文件，先删除
    final oldFile = File(savePath);
    if (await oldFile.exists()) {
      await oldFile.delete();
    }

    // 下载候选 URL 列表：Gitee 优先，GitHub 兜底
    final candidates = <String>[
      if (giteeUrl != null && giteeUrl.isNotEmpty) giteeUrl,
      url,
    ];

    Object? lastError;
    for (int i = 0; i < candidates.length; i++) {
      final downloadUrl = candidates[i];
      final isGitee = i == 0 && giteeUrl != null && giteeUrl.isNotEmpty;
      final dio = Dio(BaseOptions(
        connectTimeout: const Duration(seconds: 15),
        receiveTimeout: const Duration(minutes: 10),
      ));
      try {
        await dio.download(
          downloadUrl,
          savePath,
          onReceiveProgress: onProgress,
          options: Options(receiveTimeout: const Duration(minutes: 10)),
        );
        dio.close();
        return savePath;
      } catch (e) {
        dio.close();
        lastError = e;
        // 清理可能下载了一半的文件
        final partial = File(savePath);
        if (await partial.exists()) {
          try {
            await partial.delete();
          } catch (_) {}
        }
        // 继续尝试下一个候选 URL
      }
    }
    throw Exception('所有下载源均失败: $lastError');
  }
}
