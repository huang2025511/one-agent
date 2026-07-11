import 'package:dio/dio.dart';

import '../models/memory.dart';
import 'api_client.dart';

/// 记忆管理 API
class MemoryApi {
  /// 搜索记忆
  static Future<List<Memory>> search(String query, {int limit = 10}) async {
    final resp = await ApiClient.dio.get(
      '/api/memory/search',
      queryParameters: {'q': query, 'limit': limit},
    );
    final data = resp.data as Map<String, dynamic>;
    final results = (data['results'] as List<dynamic>? ?? [])
        .map((e) => Memory.fromApi(e as Map<String, dynamic>))
        .toList();
    return results;
  }

  /// 添加记忆
  static Future<bool> add({
    required String text,
    String? tags,
    String source = 'mobile',
  }) async {
    try {
      // 仅在 tags 非空时发送，避免服务端存储 SQL NULL
      final data = <String, dynamic>{
        'text': text,
        'source': source,
      };
      if (tags != null && tags.isNotEmpty) {
        data['tags'] = tags;
      }
      await ApiClient.dio.post('/api/memory/add', data: data);
      return true;
    } catch (_) {
      return false;
    }
  }

  /// 分页获取记忆
  static Future<MemoryPage> getPage({int page = 1, int pageSize = 20}) async {
    final resp = await ApiClient.dio.get(
      '/api/memory/page',
      queryParameters: {'page': page, 'page_size': pageSize},
    );
    final data = resp.data as Map<String, dynamic>;
    return MemoryPage(
      items: (data['items'] as List<dynamic>? ?? [])
          .map((e) => Memory.fromApi(e as Map<String, dynamic>))
          .toList(),
      total: data['total'] ?? 0,
      page: page,
      pageSize: pageSize,
    );
  }
}
