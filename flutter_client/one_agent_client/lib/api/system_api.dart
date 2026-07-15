import 'package:dio/dio.dart';

import '../models/system_stats.dart';
import 'api_client.dart';

/// 系统管理 API
class SystemApi {
  /// 获取系统统计
  static Future<SystemStats?> getStats() async {
    try {
      final resp = await ApiClient.dio.get('/api/stats');
      return SystemStats.fromJson(resp.data as Map<String, dynamic>);
    } catch (_) {
      return null;
    }
  }

  /// 获取系统健康
  static Future<SystemHealth?> getHealth() async {
    try {
      final resp = await ApiClient.dio.get('/api/health');
      return SystemHealth.fromJson(resp.data as Map<String, dynamic>);
    } catch (_) {
      return null;
    }
  }

  /// 获取配置
  static Future<AppConfig?> getConfig() async {
    try {
      final resp = await ApiClient.dio.get('/api/config');
      return AppConfig.fromJson(resp.data as Map<String, dynamic>);
    } catch (_) {
      return null;
    }
  }

  /// 获取成本统计
  static Future<CostStats?> getCosts(String period) async {
    try {
      final path = period == 'monthly' ? '/api/costs/monthly' : '/api/costs/daily';
      final resp = await ApiClient.dio.get(path);
      return CostStats.fromJson(resp.data as Map<String, dynamic>);
    } catch (_) {
      return null;
    }
  }

  /// 获取预算状态
  static Future<Map<String, dynamic>?> getBudget() async {
    try {
      final resp = await ApiClient.dio.get('/api/costs/budget');
      return resp.data as Map<String, dynamic>?;
    } catch (_) {
      return null;
    }
  }

  /// 清除缓存
  static Future<bool> clearCache() async {
    try {
      await ApiClient.dio.post('/api/cache/clear');
      return true;
    } catch (_) {
      return false;
    }
  }

  /// 更新配置
  static Future<Map<String, dynamic>?> updateConfig(Map<String, dynamic> config) async {
    try {
      final resp = await ApiClient.dio.put('/api/config', data: {'config': config});
      return resp.data as Map<String, dynamic>?;
    } catch (_) {
      return null;
    }
  }

  /// 获取审计日志
  static Future<List<Map<String, dynamic>>> getAuditLogs({int limit = 50}) async {
    try {
      final resp = await ApiClient.dio.get('/api/audit', queryParameters: {'limit': limit});
      final data = resp.data as Map<String, dynamic>;
      return (data['entries'] as List<dynamic>? ?? [])
          .cast<Map<String, dynamic>>();
    } catch (_) {
      return [];
    }
  }

  /// 获取模型目录（含分类、4层路由映射、路由统计）
  static Future<Map<String, dynamic>?> getModels() async {
    try {
      final resp = await ApiClient.dio.get('/api/models');
      return resp.data as Map<String, dynamic>?;
    } catch (_) {
      return null;
    }
  }

  /// 获取日志
  /// since: Unix 时间戳（秒）。传 0 表示查看全部历史日志；
  /// 不传或传 null 表示只看本次启动以来的日志（服务端默认行为）。
  static Future<Map<String, dynamic>?> getLogs({
    int tail = 200,
    String? level,
    String? search,
    double? since,
  }) async {
    try {
      final params = <String, dynamic>{'tail': tail};
      if (level != null) params['level'] = level;
      if (search != null) params['search'] = search;
      if (since != null) params['since'] = since;
      final resp = await ApiClient.dio.get('/api/logs', queryParameters: params);
      return resp.data as Map<String, dynamic>?;
    } catch (_) {
      return null;
    }
  }

  /// 测试模型
  static Future<Map<String, dynamic>?> testModel(String model, {String apiKey = ''}) async {
    try {
      final resp = await ApiClient.dio.post('/api/models/test', data: {
        'model': model,
        if (apiKey.isNotEmpty) 'api_key': apiKey,
      });
      return resp.data as Map<String, dynamic>?;
    } catch (_) {
      return null;
    }
  }

  /// 列出所有已知服务商
  static Future<Map<String, dynamic>?> listProviders() async {
    try {
      final resp = await ApiClient.dio.post('/api/models/providers');
      return resp.data as Map<String, dynamic>?;
    } catch (_) {
      return null;
    }
  }

  /// 测试服务商连通性
  static Future<Map<String, dynamic>?> testProvider({
    required String provider,
    String apiKey = '',
    String baseUrl = '',
  }) async {
    try {
      final resp = await ApiClient.dio.post('/api/models/providers/test', data: {
        'provider': provider,
        if (apiKey.isNotEmpty) 'api_key': apiKey,
        if (baseUrl.isNotEmpty) 'base_url': baseUrl,
      });
      return resp.data as Map<String, dynamic>?;
    } catch (_) {
      return null;
    }
  }

  /// 浏览公开社区市场
  static Future<Map<String, dynamic>?> browseMarketplace({String query = ''}) async {
    try {
      final resp = await ApiClient.dio.get('/api/marketplace/browse', queryParameters: {'query': query});
      return resp.data as Map<String, dynamic>?;
    } catch (_) {
      return null;
    }
  }

  /// 从 URL 安装技能
  static Future<Map<String, dynamic>?> installFromUrl(String source) async {
    try {
      final resp = await ApiClient.dio.post('/api/marketplace/install_url', data: {'source': source});
      return resp.data as Map<String, dynamic>?;
    } catch (_) {
      return null;
    }
  }
}
