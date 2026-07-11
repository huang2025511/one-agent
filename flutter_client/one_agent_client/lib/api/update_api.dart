import 'package:dio/dio.dart';
import 'package:path_provider/path_provider.dart';
import 'dart:io';

/// GitHub Release 信息
class ReleaseInfo {
  final String tagName;      // e.g. "app-v24"
  final String name;         // e.g. "One-Agent App v24"
  final String? body;        // Release notes
  final String? publishedAt; // ISO8601
  final String apkUrl;       // 下载地址
  final int apkSize;         // APK 字节数

  const ReleaseInfo({
    required this.tagName,
    required this.name,
    this.body,
    this.publishedAt,
    required this.apkUrl,
    required this.apkSize,
  });

  /// 从 tag_name 解析数字版本号（如 "app-v24" → 24）
  int get versionNumber {
    final m = RegExp(r'(\d+)\s*$').firstMatch(tagName);
    return m != null ? int.parse(m.group(1)!) : 0;
  }
}

/// 应用更新 API — 调用 GitHub Releases
class UpdateApi {
  static const String _repo = 'huang2025511/one-agent';

  /// 获取最新 Release
  /// [currentVersion] 当前应用版本号（用于无新版本时返回 null）
  static Future<ReleaseInfo?> getLatestRelease({int? currentVersion}) async {
    final dio = Dio(BaseOptions(
      connectTimeout: const Duration(seconds: 15),
      receiveTimeout: const Duration(seconds: 15),
      headers: {'Accept': 'application/vnd.github+json'},
    ));

    try {
      final resp = await dio.get(
        'https://api.github.com/repos/$_repo/releases/latest',
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

      final release = ReleaseInfo(
        tagName: tagName,
        name: name,
        body: body,
        publishedAt: publishedAt,
        apkUrl: apkUrl,
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
  /// [onProgress] 接收 (received, total) 用于显示进度
  static Future<String> downloadApk(
    String url, {
    void Function(int received, int total)? onProgress,
  }) async {
    final dio = Dio(BaseOptions(
      connectTimeout: const Duration(seconds: 30),
      receiveTimeout: const Duration(minutes: 10),
    ));

    try {
      final tempDir = await getTemporaryDirectory();
      final savePath = '${tempDir.path}/one-agent-update.apk';

      // 如果已存在旧文件，先删除
      final oldFile = File(savePath);
      if (await oldFile.exists()) {
        await oldFile.delete();
      }

      await dio.download(
        url,
        savePath,
        onReceiveProgress: onProgress,
        options: Options(receiveTimeout: const Duration(minutes: 10)),
      );
      return savePath;
    } finally {
      dio.close();
    }
  }
}
