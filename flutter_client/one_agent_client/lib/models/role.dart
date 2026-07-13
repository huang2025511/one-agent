import 'package:freezed_annotation/freezed_annotation.dart';

part 'role.freezed.dart';
part 'role.g.dart';

/// 角色（人格设定）
@freezed
class Role with _$Role {
  const factory Role({
    required int id,
    required String name,
    @Default('') String description,
    @Default('') String systemPromptOverride,
    @Default('🤖') String icon,
    @Default('#6750A4') String color,
    @Default(false) bool isActive,
    DateTime? createdAt,
    DateTime? updatedAt,
  }) = _Role;

  factory Role.fromJson(Map<String, dynamic> json) => _$RoleFromJson(json);

  const Role._();

  factory Role.fromApi(Map<String, dynamic> json) {
    return Role(
      id: json['id'] as int,
      name: json['name'] as String? ?? '',
      description: json['description'] as String? ?? '',
      systemPromptOverride: json['system_prompt_override'] as String? ?? '',
      icon: json['icon'] as String? ?? '🤖',
      color: json['color'] as String? ?? '#6750A4',
      isActive: (json['is_active'] as int?) == 1,
      createdAt: _parseTs(json['created_at']),
      updatedAt: _parseTs(json['updated_at']),
    );
  }

  static DateTime? _parseTs(dynamic v) {
    if (v == null) return null;
    if (v is num) {
      return DateTime.fromMillisecondsSinceEpoch(
        v > 1e12 ? v.toInt() : (v * 1000).toInt(),
      );
    }
    return DateTime.tryParse(v.toString());
  }
}
