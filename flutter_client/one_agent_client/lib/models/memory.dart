import 'package:freezed_annotation/freezed_annotation.dart';

part 'memory.freezed.dart';
part 'memory.g.dart';

/// 记忆条目
@freezed
class Memory with _$Memory {
  const factory Memory({
    required int id,
    required String text,
    String? source,
    String? tags,
    DateTime? createdAt,
    double? relevance,
  }) = _Memory;

  factory Memory.fromJson(Map<String, dynamic> json) =>
      _$MemoryFromJson(json);

  const Memory._();

  factory Memory.fromApi(Map<String, dynamic> json) {
    // 服务端 /api/memory/search 返回 id 为字符串（str(rowid)），
    // /api/memory/page 的条目则没有 id 字段。统一安全解析为 int。
    final rawId = json['id'];
    final id = rawId is int
        ? rawId
        : int.tryParse(rawId?.toString() ?? '') ?? 0;
    // 服务端用 timestamp（float epoch）而非 created_at（ISO 字符串）
    final rawTs = json['timestamp'] ?? json['created_at'];
    final createdAt = _parseTimestamp(rawTs);
    return Memory(
      id: id,
      text: json['text'] ?? json['content'] ?? '',
      source: json['source'],
      tags: json['tags'],
      createdAt: createdAt,
      // 服务端用 weight 表示相关度
      relevance: (json['weight'] ?? json['relevance'])?.toDouble(),
    );
  }

  static DateTime? _parseTimestamp(dynamic value) {
    if (value == null) return null;
    if (value is int) {
      return DateTime.fromMillisecondsSinceEpoch(
        value > 1e12 ? value : value * 1000,
      );
    }
    if (value is double) {
      return DateTime.fromMillisecondsSinceEpoch(
        (value > 1e12 ? value : value * 1000).toInt(),
      );
    }
    return DateTime.tryParse(value.toString());
  }
}

/// 记忆分页结果
@freezed
class MemoryPage with _$MemoryPage {
  const factory MemoryPage({
    required List<Memory> items,
    required int total,
    required int page,
    required int pageSize,
  }) = _MemoryPage;

  factory MemoryPage.fromJson(Map<String, dynamic> json) =>
      _$MemoryPageFromJson(json);
}
