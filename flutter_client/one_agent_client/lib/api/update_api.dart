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
  /// 国内用户优先从 Gitee API 获取（更快更稳定），失败时回退到 GitHub API
  /// [currentVersion] 当前应用版本号（用于无新版本时返回 null）
  /// 两个源都不可用时抛异常（而非返回 null），让 UI 显示"检测失败"而非"最新版本"
  static Future<ReleaseInfo?> getLatestRelease({int? currentVersion}) async {
    final release = await fetchLatestRelease();
    if (release == null) {
      throw Exception('无法连接更新服务器（Gitee 和 GitHub 均不可达），请检查网络后重试');
    }
    // 比较版本号：若当前版本 >= 最新版本，则无需更新
    if (currentVersion != null && currentVersion >= release.versionNumber) {
      return null;
    }
    return release;
  }

  /// 问题4 修复：始终获取最新 Release 信息（不做版本号过滤）。
  ///
  /// 之前 getLatestRelease 在 currentVersion >= release.versionNumber 时
  /// 返回 null，导致 UI 无法显示"最新版本是什么"。如果用户的 buildNumber
  /// 因构建配置问题意外大于服务端 tag（例如 debug 构建用了错误的
  /// build-number），就永远看不到更新提示。
  ///
  /// 现在 fetchLatestRelease 始终返回 release（只要网络可达且有 APK 资源），
  /// 由 UpdateState.hasUpdate 根据 versionNumber 比较决定是否提示更新。
  /// 这样 UI 可以同时显示"当前版本"和"服务端最新版本"，用户也能手动
  /// 强制下载（即使版本号比较认为"无需更新"）。
  static Future<ReleaseInfo?> fetchLatestRelease() async {
    // 国内用户优先 Gitee（1-2秒即可响应），失败回退 GitHub
    ReleaseInfo? release = await _fetchFromGitee();
    release ??= await _fetchFromGitHub();
    return release;
  }

  /// 从 GitHub API 获取最新 Release
  static Future<ReleaseInfo?> _fetchFromGitHub() async {
    final dio = Dio(BaseOptions(
      connectTimeout: const Duration(seconds: 15),
      receiveTimeout: const Duration(seconds: 15),
      headers: {'Accept': 'application/vnd.github+json'},
    ));

    try {
      final resp = await dio.get(
        'https://api.github.com/repos/$_ghRepo/releases/latest',
      );
      final data = resp.data as Map<String, dynamic>;
      final tagName = data['tag_name'] as String? ?? '';
      final name = data['name'] as String? ?? tagName;
      final body = data['body'] as String?;
      final publishedAt = data['published_at'] as String?;

      String? apkUrl;
      String? apkAssetName;
      int apkSize = 0;
      for (final asset in (data['assets'] as List<dynamic>? ?? [])) {
        final assetName = asset['name'] as String? ?? '';
        if (assetName.endsWith('.apk')) {
          apkUrl = asset['browser_download_url'] as String?;
          apkAssetName = assetName;
          apkSize = (asset['size'] as num?)?.toInt() ?? 0;
          // 优先取 arm64-v8a（主流架构）
          if (assetName.contains('arm64-v8a')) break;
        }
      }

      if (apkUrl == null) return null;

      // 用 GitHub 的 APK 文件名构造 Gitee 下载地址
      final giteeApkUrl = apkAssetName != null
          ? 'https://gitee.com/$_giteeRepo/releases/download/$tagName/$apkAssetName'
          : null;

      return ReleaseInfo(
        tagName: tagName,
        name: name,
        body: body,
        publishedAt: publishedAt,
        apkUrl: apkUrl,
        giteeApkUrl: giteeApkUrl,
        apkSize: apkSize,
      );
    } on DioException {
      return null; // GitHub 不可用，返回 null 让调用方回退 Gitee
    } finally {
      dio.close();
    }
  }

  /// 从 Gitee API 获取最新 Release（国内网络更稳定）
  static Future<ReleaseInfo?> _fetchFromGitee() async {
    final dio = Dio(BaseOptions(
      connectTimeout: const Duration(seconds: 15),
      receiveTimeout: const Duration(seconds: 15),
    ));

    try {
      final resp = await dio.get(
        'https://gitee.com/api/v5/repos/$_giteeRepo/releases/latest',
      );
      final data = resp.data as Map<String, dynamic>;
      final tagName = data['tag_name'] as String? ?? '';
      final name = data['name'] as String? ?? tagName;
      final body = data['body'] as String?;
      final publishedAt = data['created_at'] as String?;

      String? apkUrl;
      String? giteeApkUrl;
      int apkSize = 0;
      for (final asset in (data['assets'] as List<dynamic>? ?? [])) {
        final assetName = asset['name'] as String? ?? '';
        if (assetName.endsWith('.apk')) {
          giteeApkUrl = asset['browser_download_url'] as String?;
          apkSize = (asset['size'] as num?)?.toInt() ?? 0;
          // 优先取 arm64-v8a（主流架构）
          if (assetName.contains('arm64-v8a')) break;
        }
      }

      if (giteeApkUrl == null) return null;

      // 用 Gitee 文件名构造 GitHub 下载地址（作为备用）
      final ghApkUrl = apkUrl ??
          'https://github.com/$_ghRepo/releases/download/$tagName/${giteeApkUrl.split('/').last}';

      return ReleaseInfo(
        tagName: tagName,
        name: name,
        body: body,
        publishedAt: publishedAt,
        apkUrl: ghApkUrl,
        giteeApkUrl: giteeApkUrl,
        apkSize: apkSize,
      );
    } on DioException {
      return null;
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

  /// 断点续传下载 APK
  /// 支持从已下载的位置继续下载，使用 HTTP Range header
  /// 下载到临时文件（.apk.tmp），完成后重命名为最终文件（.apk）
  ///
  /// 处理以下情况：
  /// - 服务端不支持 Range 请求（返回 200 而非 206）：自动截断临时文件重新下载
  /// - Gitee URL 作为备用下载源：Gitee 优先，GitHub 兜底
  /// - 下载中断后保留临时文件：下次调用可从已下载位置继续
  /// - 正确的进度回调：received/total 包含已下载+本次下载的总量
  static Future<String> downloadApkWithResume(
    String url, {
    String? giteeUrl,
    void Function(int received, int total, bool isResumed)? onProgress,
  }) async {
    final dir = await getTemporaryDirectory();
    final tempPath = '${dir.path}/one_agent_update.apk.tmp';
    final finalPath = '${dir.path}/one_agent_update.apk';

    final tempFile = File(tempPath);
    final finalFile = File(finalPath);

    // 下载候选 URL：Gitee 优先，GitHub 兜底
    final candidates = <String>[
      if (giteeUrl != null && giteeUrl.isNotEmpty) giteeUrl,
      url,
    ];

    // 1. 通过 HEAD 获取文件总大小（依次尝试各候选源）
    int? totalSize;
    for (final candidate in candidates) {
      final headDio = Dio(BaseOptions(
        connectTimeout: const Duration(seconds: 15),
        receiveTimeout: const Duration(seconds: 15),
        followRedirects: true,
      ));
      try {
        final headResp = await headDio.head(
          candidate,
          options: Options(
            validateStatus: (s) => s != null && s < 400,
            headers: {'Accept': '*/*'},
          ),
        );
        final len = int.tryParse(
          headResp.headers.value('content-length') ?? '',
        );
        if (len != null && len > 0) {
          totalSize = len;
          break;
        }
      } catch (_) {
        // 某些服务器不支持 HEAD，忽略错误继续尝试下一个源
      } finally {
        headDio.close();
      }
    }

    // 2. 检查临时文件已下载字节数
    int downloadedBytes = 0;
    if (await tempFile.exists()) {
      downloadedBytes = await tempFile.length();
    }

    // 临时文件比总大小还大，说明数据损坏，删除后重新下载
    if (totalSize != null &&
        downloadedBytes > totalSize &&
        totalSize > 0) {
      try {
        await tempFile.delete();
      } catch (_) {}
      downloadedBytes = 0;
    }

    // 3. 若已下载完成，直接重命名为最终文件并返回
    if (totalSize != null &&
        downloadedBytes >= totalSize &&
        totalSize > 0 &&
        downloadedBytes > 0) {
      if (await finalFile.exists()) {
        try {
          await finalFile.delete();
        } catch (_) {}
      }
      await tempFile.rename(finalPath);
      return finalPath;
    }

    // 4. 使用 Range header 流式下载剩余部分，逐个尝试候选源
    Object? lastError;
    for (int i = 0; i < candidates.length; i++) {
      final downloadUrl = candidates[i];
      final dio = Dio(BaseOptions(
        connectTimeout: const Duration(seconds: 15),
        receiveTimeout: const Duration(minutes: 30),
        followRedirects: true,
      ));

      try {
        // 每轮循环开始前重新读取临时文件大小（上一轮可能已写入部分数据）
        if (await tempFile.exists()) {
          downloadedBytes = await tempFile.length();
        } else {
          downloadedBytes = 0;
        }

        final isResumed = downloadedBytes > 0;

        final response = await dio.get(
          downloadUrl,
          options: Options(
            headers: downloadedBytes > 0
                ? {'Range': 'bytes=$downloadedBytes-'}
                : null,
            responseType: ResponseType.stream,
            receiveTimeout: const Duration(minutes: 30),
            // 接受 200 / 206；416 表示 Range 越界（视为已完成）
            validateStatus: (s) => s != null && (s < 300 || s == 416),
          ),
        );

        final statusCode = response.statusCode ?? 200;

        // 416 Range Not Satisfiable：临时文件已包含全部内容
        if (statusCode == 416) {
          dio.close();
          if (await finalFile.exists()) {
            try {
              await finalFile.delete();
            } catch (_) {}
          }
          await tempFile.rename(finalPath);
          return finalPath;
        }

        final supportsResume = statusCode == 206;

        // 服务端返回 200（不支持 Range）但本地有部分数据：
        // 必须截断临时文件重新下载
        IOSink sink;
        int startBytes;
        if (supportsResume && downloadedBytes > 0) {
          sink = tempFile.openWrite(mode: FileMode.append);
          startBytes = downloadedBytes;
        } else {
          sink = tempFile.openWrite(mode: FileMode.writeOnly);
          startBytes = 0;
          downloadedBytes = 0;
        }

        // 计算实际总大小
        final contentLength = int.tryParse(
              response.headers.value('content-length') ?? '',
            ) ??
            0;
        final actualTotal = supportsResume
            ? (totalSize ?? (startBytes + contentLength))
            : (totalSize ?? contentLength);

        try {
          int currentBytes = startBytes;
          await response.data.stream.forEach((List<int> chunk) {
            sink.add(chunk);
            currentBytes += chunk.length;
            onProgress?.call(currentBytes, actualTotal, isResumed);
          });
          await sink.flush();
          await sink.close();

          // 完整性校验：若已知总大小，校验最终字节数
          if (actualTotal > 0 && currentBytes < actualTotal) {
            throw DioException(
              requestOptions: response.requestOptions,
              message: '下载不完整: $currentBytes / $actualTotal',
            );
          }

          dio.close();

          // 重命名为最终文件
          if (await finalFile.exists()) {
            try {
              await finalFile.delete();
            } catch (_) {}
          }
          await tempFile.rename(finalPath);
          return finalPath;
        } catch (e) {
          try {
            await sink.close();
          } catch (_) {}
          rethrow;
        }
      } catch (e) {
        dio.close();
        lastError = e;
        // 不删除临时文件，保留以便下一轮或下次启动时续传
      }
    }

    throw Exception('所有下载源均失败: $lastError');
  }
}
