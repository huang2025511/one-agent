// GENERATED CODE - DO NOT MODIFY BY HAND

part of 'skill.dart';

// **************************************************************************
// JsonSerializableGenerator
// **************************************************************************

_$SkillImpl _$$SkillImplFromJson(Map<String, dynamic> json) => _$SkillImpl(
      id: json['id'] as String,
      title: json['title'] as String,
      description: json['description'] as String?,
      version: json['version'] as String?,
      uses: (json['uses'] as num?)?.toInt(),
      lastUsed: json['lastUsed'] == null
          ? null
          : DateTime.parse(json['lastUsed'] as String),
      schema: json['schema'] as Map<String, dynamic>?,
      isBuiltin: json['isBuiltin'] as bool?,
      isProcedural: json['isProcedural'] as bool?,
    );

Map<String, dynamic> _$$SkillImplToJson(_$SkillImpl instance) =>
    <String, dynamic>{
      'id': instance.id,
      'title': instance.title,
      'description': instance.description,
      'version': instance.version,
      'uses': instance.uses,
      'lastUsed': instance.lastUsed?.toIso8601String(),
      'schema': instance.schema,
      'isBuiltin': instance.isBuiltin,
      'isProcedural': instance.isProcedural,
    };

_$MarketplacePackageImpl _$$MarketplacePackageImplFromJson(
        Map<String, dynamic> json) =>
    _$MarketplacePackageImpl(
      name: json['name'] as String,
      description: json['description'] as String,
      version: json['version'] as String?,
      author: json['author'] as String?,
      downloads: (json['downloads'] as num?)?.toInt(),
      tags: (json['tags'] as List<dynamic>?)?.map((e) => e as String).toList(),
      installed: json['installed'] as bool?,
    );

Map<String, dynamic> _$$MarketplacePackageImplToJson(
        _$MarketplacePackageImpl instance) =>
    <String, dynamic>{
      'name': instance.name,
      'description': instance.description,
      'version': instance.version,
      'author': instance.author,
      'downloads': instance.downloads,
      'tags': instance.tags,
      'installed': instance.installed,
    };
