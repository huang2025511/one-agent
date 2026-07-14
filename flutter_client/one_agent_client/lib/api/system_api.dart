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
}
