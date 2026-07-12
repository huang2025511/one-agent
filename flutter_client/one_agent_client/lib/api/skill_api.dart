import '../models/skill.dart';
import 'api_client.dart';

/// 技能管理 API
class SkillApi {
  /// 获取技能列表
  static Future<List<Skill>> listSkills() async {
    final resp = await ApiClient.dio.get('/api/skills');
    final data = resp.data as Map<String, dynamic>;
    final skills = data['skills'] as List<dynamic>? ?? [];
    return skills.map((e) {
      final id = e is String
          ? e
          : (e is Map ? e['id']?.toString() ?? '' : e.toString());
      final detail = e is Map ? Map<String, dynamic>.from(e) : null;
      return Skill.fromApi(id, detail);
    }).toList();
  }

  /// 获取市场包列表
  static Future<List<MarketplacePackage>> listMarketplace({String query = ''}) async {
    try {
      final resp = await ApiClient.dio.get(
        '/api/marketplace',
        queryParameters: {'query': query},
      );
      final data = resp.data as Map<String, dynamic>;
      return (data['packages'] as List<dynamic>? ?? [])
          .map((e) => MarketplacePackage.fromJson(e as Map<String, dynamic>))
          .toList();
    } catch (_) {
      return [];
    }
  }

  /// 安装技能
  /// 修复：服务端 install_skill(name, target_dir) 是查询参数（FastAPI 简单类型默认 query param），
  /// 之前放在 POST body 中导致 422 错误，技能安装功能完全不可用
  static Future<bool> install(String name, {String? targetDir}) async {
    try {
      final queryParameters = <String, dynamic>{'name': name};
      if (targetDir != null) queryParameters['target_dir'] = targetDir;
      await ApiClient.dio.post(
        '/api/marketplace/install',
        queryParameters: queryParameters,
      );
      return true;
    } catch (_) {
      return false;
    }
  }

  /// 卸载技能
  /// 修复：使用 Dio 的 queryParameters 自动 URL 编码，之前手动拼接 `?target_dir=$targetDir`
  /// 若 targetDir 含特殊字符会破坏 URL 结构
  static Future<bool> uninstall(String name, {String? targetDir}) async {
    try {
      final queryParameters = <String, dynamic>{};
      if (targetDir != null) queryParameters['target_dir'] = targetDir;
      await ApiClient.dio.delete(
        '/api/marketplace/$name',
        queryParameters: queryParameters.isNotEmpty ? queryParameters : null,
      );
      return true;
    } catch (_) {
      return false;
    }
  }
}
