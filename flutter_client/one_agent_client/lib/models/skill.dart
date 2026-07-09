import 'package:freezed_annotation/freezed_annotation.dart';

part 'skill.freezed.dart';
part 'skill.g.dart';

/// 技能模型
@freezed
class Skill with _$Skill {
  const factory Skill({
    required String id,
    required String title,
    String? description,
    String? version,
    int? uses,
    DateTime? lastUsed,
    Map<String, dynamic>? schema,
    bool? isBuiltin,
    bool? isProcedural,
  }) = _Skill;

  factory Skill.fromJson(Map<String, dynamic> json) =>
      _$SkillFromJson(json);

  const Skill._();

  factory Skill.fromApi(String id, Map<String, dynamic>? detail) {
    return Skill(
      id: id,
      title: detail?['title'] ?? id,
      description: detail?['description'],
      version: detail?['version'],
      uses: detail?['uses'],
      schema: detail?['schema'],
    );
  }
}

/// 市场包
@freezed
class MarketplacePackage with _$MarketplacePackage {
  const factory MarketplacePackage({
    required String name,
    required String description,
    String? version,
    String? author,
    int? downloads,
    List<String>? tags,
    bool? installed,
  }) = _MarketplacePackage;

  factory MarketplacePackage.fromJson(Map<String, dynamic> json) =>
      _$MarketplacePackageFromJson(json);
}
