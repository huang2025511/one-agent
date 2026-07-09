import 'package:dio/dio.dart';

import '../models/approval.dart';
import 'api_client.dart';

/// 审批管理 API
class ApprovalApi {
  /// 获取待审批列表
  static Future<List<ApprovalRequest>> listPending() async {
    try {
      final resp = await ApiClient.dio.get('/api/approvals/pending');
      final data = resp.data as Map<String, dynamic>;
      final pending = (data['pending'] as List<dynamic>? ?? [])
          .map((e) => ApprovalRequest.fromApi(e as Map<String, dynamic>))
          .toList();
      return pending;
    } catch (_) {
      return [];
    }
  }

  /// 批准请求
  static Future<bool> approve(String requestId) async {
    try {
      await ApiClient.dio.post('/api/approvals/$requestId/approve');
      return true;
    } catch (_) {
      return false;
    }
  }

  /// 拒绝请求
  static Future<bool> deny(String requestId) async {
    try {
      await ApiClient.dio.post('/api/approvals/$requestId/deny');
      return true;
    } catch (_) {
      return false;
    }
  }
}
