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
    return Memory(
      id: json['id'] ?? 0,
      text: json['text'] ?? json['content'] ?? '',
      source: json['source'],
      tags: json['tags'],
      createdAt: json['created_at'] != null
          ? DateTime.tryParse(json['created_at'].toString())
          : null,
      relevance: json['relevance']?.toDouble(),
    );
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
