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
  static Future<bool> install(String name, {String? targetDir}) async {
    try {
      final data = {'name': name};
      if (targetDir != null) data['target_dir'] = targetDir;
      await ApiClient.dio.post('/api/marketplace/install', data: data);
      return true;
    } catch (_) {
      return false;
    }
  }

  /// 卸载技能
  static Future<bool> uninstall(String name, {String? targetDir}) async {
    try {
      final query = targetDir != null ? '?target_dir=$targetDir' : '';
      await ApiClient.dio.delete('/api/marketplace/$name$query');
      return true;
    } catch (_) {
      return false;
    }
  }
}
