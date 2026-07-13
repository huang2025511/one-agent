import '../models/role.dart';
import 'api_client.dart';

/// 角色 API 客户端
class RoleApi {
  /// 获取所有角色
  static Future<List<Role>> list() async {
    final resp = await ApiClient.dio.get('/api/roles');
    final list = resp.data['roles'] as List? ?? [];
    return list.map((e) => Role.fromApi(e as Map<String, dynamic>)).toList();
  }

  /// 创建角色
  static Future<Role> create({
    required String name,
    String description = '',
    String systemPromptOverride = '',
    String icon = '🤖',
    String color = '#6750A4',
  }) async {
    final resp = await ApiClient.dio.post('/api/roles', data: {
      'name': name,
      'description': description,
      'system_prompt_override': systemPromptOverride,
      'icon': icon,
      'color': color,
    });
    return Role.fromApi(resp.data['role'] as Map<String, dynamic>);
  }

  /// 更新角色
  static Future<Role> update(int id, {
    String? name,
    String? description,
    String? systemPromptOverride,
    String? icon,
    String? color,
  }) async {
    final body = <String, dynamic>{};
    if (name != null) body['name'] = name;
    if (description != null) body['description'] = description;
    if (systemPromptOverride != null) body['system_prompt_override'] = systemPromptOverride;
    if (icon != null) body['icon'] = icon;
    if (color != null) body['color'] = color;
    final resp = await ApiClient.dio.put('/api/roles/$id', data: body);
    return Role.fromApi(resp.data['role'] as Map<String, dynamic>);
  }

  /// 删除角色
  static Future<bool> delete(int id) async {
    await ApiClient.dio.delete('/api/roles/$id');
    return true;
  }

  /// 激活角色
  static Future<bool> activate(int id) async {
    await ApiClient.dio.post('/api/roles/$id/activate');
    return true;
  }

  /// 取消所有活跃角色（回到默认 One-Agent 人格）
  static Future<bool> deactivate() async {
    await ApiClient.dio.post('/api/roles/deactivate');
    return true;
  }

  /// 获取当前活跃角色
  static Future<Role?> getActive() async {
    final resp = await ApiClient.dio.get('/api/roles/active');
    final role = resp.data['role'];
    if (role == null) return null;
    return Role.fromApi(role as Map<String, dynamic>);
  }
}
