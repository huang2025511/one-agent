// GENERATED CODE - DO NOT MODIFY BY HAND

part of 'memory.dart';

// **************************************************************************
// JsonSerializableGenerator
// **************************************************************************

_$MemoryImpl _$$MemoryImplFromJson(Map<String, dynamic> json) => _$MemoryImpl(
      id: (json['id'] as num).toInt(),
      text: json['text'] as String,
      source: json['source'] as String?,
      tags: json['tags'] as String?,
      createdAt: json['createdAt'] == null
          ? null
          : DateTime.parse(json['createdAt'] as String),
      relevance: (json['relevance'] as num?)?.toDouble(),
    );

Map<String, dynamic> _$$MemoryImplToJson(_$MemoryImpl instance) =>
    <String, dynamic>{
      'id': instance.id,
      'text': instance.text,
      'source': instance.source,
      'tags': instance.tags,
      'createdAt': instance.createdAt?.toIso8601String(),
      'relevance': instance.relevance,
    };

_$MemoryPageImpl _$$MemoryPageImplFromJson(Map<String, dynamic> json) =>
    _$MemoryPageImpl(
      items: (json['items'] as List<dynamic>)
          .map((e) => Memory.fromJson(e as Map<String, dynamic>))
          .toList(),
      total: (json['total'] as num).toInt(),
      page: (json['page'] as num).toInt(),
      pageSize: (json['pageSize'] as num).toInt(),
    );

Map<String, dynamic> _$$MemoryPageImplToJson(_$MemoryPageImpl instance) =>
    <String, dynamic>{
      'items': instance.items,
      'total': instance.total,
      'page': instance.page,
      'pageSize': instance.pageSize,
    };
