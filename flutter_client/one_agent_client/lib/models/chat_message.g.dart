// GENERATED CODE - DO NOT MODIFY BY HAND

part of 'chat_message.dart';

// **************************************************************************
// JsonSerializableGenerator
// **************************************************************************

_$ChatMessageImpl _$$ChatMessageImplFromJson(Map<String, dynamic> json) =>
    _$ChatMessageImpl(
      id: json['id'] as String,
      role: $enumDecode(_$MessageRoleEnumMap, json['role']),
      content: json['content'] as String,
      thinking: json['thinking'] as String?,
      sessionId: json['sessionId'] as String?,
      timestamp: json['timestamp'] == null
          ? null
          : DateTime.parse(json['timestamp'] as String),
      isStreaming: json['isStreaming'] as bool?,
      isError: json['isError'] as bool?,
      errorMessage: json['errorMessage'] as String?,
      metadata: json['metadata'] as Map<String, dynamic>?,
    );

Map<String, dynamic> _$$ChatMessageImplToJson(_$ChatMessageImpl instance) =>
    <String, dynamic>{
      'id': instance.id,
      'role': _$MessageRoleEnumMap[instance.role]!,
      'content': instance.content,
      'thinking': instance.thinking,
      'sessionId': instance.sessionId,
      'timestamp': instance.timestamp?.toIso8601String(),
      'isStreaming': instance.isStreaming,
      'isError': instance.isError,
      'errorMessage': instance.errorMessage,
      'metadata': instance.metadata,
    };

const _$MessageRoleEnumMap = {
  MessageRole.user: 'user',
  MessageRole.assistant: 'assistant',
  MessageRole.system: 'system',
  MessageRole.thinking: 'thinking',
  MessageRole.tool: 'tool',
};

_$StreamEventImpl _$$StreamEventImplFromJson(Map<String, dynamic> json) =>
    _$StreamEventImpl(
      type: json['type'] as String,
      content: json['content'] as String?,
      status: json['status'] as String?,
      sessionId: json['sessionId'] as String?,
      done: json['done'] as bool?,
      metadata: json['metadata'] as Map<String, dynamic>?,
    );

Map<String, dynamic> _$$StreamEventImplToJson(_$StreamEventImpl instance) =>
    <String, dynamic>{
      'type': instance.type,
      'content': instance.content,
      'status': instance.status,
      'sessionId': instance.sessionId,
      'done': instance.done,
      'metadata': instance.metadata,
    };
