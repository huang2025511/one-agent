// coverage:ignore-file
// GENERATED CODE - DO NOT MODIFY BY HAND
// ignore_for_file: type=lint
// ignore_for_file: unused_element, deprecated_member_use, deprecated_member_use_from_same_package, use_function_type_syntax_for_parameters, unnecessary_const, avoid_init_to_null, invalid_override_different_default_values_named, prefer_expression_function_bodies, annotate_overrides, invalid_annotation_target, unnecessary_question_mark

part of 'chat_message.dart';

// **************************************************************************
// FreezedGenerator
// **************************************************************************

T _$identity<T>(T value) => value;

final _privateConstructorUsedError = UnsupportedError(
    'It seems like you constructed your class using `MyClass._()`. This constructor is only meant to be used by freezed and you are not supposed to need it nor use it.\nPlease check the documentation here for more information: https://github.com/rrousselGit/freezed#adding-getters-and-methods-to-our-models');

ChatMessage _$ChatMessageFromJson(Map<String, dynamic> json) {
  return _ChatMessage.fromJson(json);
}

/// @nodoc
mixin _$ChatMessage {
  String get id => throw _privateConstructorUsedError;
  MessageRole get role => throw _privateConstructorUsedError;
  String get content => throw _privateConstructorUsedError;
  String? get thinking => throw _privateConstructorUsedError;
  String? get sessionId => throw _privateConstructorUsedError;
  DateTime? get timestamp => throw _privateConstructorUsedError;
  bool? get isStreaming => throw _privateConstructorUsedError;
  bool? get isError => throw _privateConstructorUsedError;
  String? get errorMessage => throw _privateConstructorUsedError;
  Map<String, dynamic>? get metadata => throw _privateConstructorUsedError;

  Map<String, dynamic> toJson() => throw _privateConstructorUsedError;
  @JsonKey(ignore: true)
  $ChatMessageCopyWith<ChatMessage> get copyWith =>
      throw _privateConstructorUsedError;
}

/// @nodoc
abstract class $ChatMessageCopyWith<$Res> {
  factory $ChatMessageCopyWith(
          ChatMessage value, $Res Function(ChatMessage) then) =
      _$ChatMessageCopyWithImpl<$Res, ChatMessage>;
  @useResult
  $Res call(
      {String id,
      MessageRole role,
      String content,
      String? thinking,
      String? sessionId,
      DateTime? timestamp,
      bool? isStreaming,
      bool? isError,
      String? errorMessage,
      Map<String, dynamic>? metadata});
}

/// @nodoc
class _$ChatMessageCopyWithImpl<$Res, $Val extends ChatMessage>
    implements $ChatMessageCopyWith<$Res> {
  _$ChatMessageCopyWithImpl(this._value, this._then);

  // ignore: unused_field
  final $Val _value;
  // ignore: unused_field
  final $Res Function($Val) _then;

  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? id = null,
    Object? role = null,
    Object? content = null,
    Object? thinking = freezed,
    Object? sessionId = freezed,
    Object? timestamp = freezed,
    Object? isStreaming = freezed,
    Object? isError = freezed,
    Object? errorMessage = freezed,
    Object? metadata = freezed,
  }) {
    return _then(_value.copyWith(
      id: null == id
          ? _value.id
          : id // ignore: cast_nullable_to_non_nullable
              as String,
      role: null == role
          ? _value.role
          : role // ignore: cast_nullable_to_non_nullable
              as MessageRole,
      content: null == content
          ? _value.content
          : content // ignore: cast_nullable_to_non_nullable
              as String,
      thinking: freezed == thinking
          ? _value.thinking
          : thinking // ignore: cast_nullable_to_non_nullable
              as String?,
      sessionId: freezed == sessionId
          ? _value.sessionId
          : sessionId // ignore: cast_nullable_to_non_nullable
              as String?,
      timestamp: freezed == timestamp
          ? _value.timestamp
          : timestamp // ignore: cast_nullable_to_non_nullable
              as DateTime?,
      isStreaming: freezed == isStreaming
          ? _value.isStreaming
          : isStreaming // ignore: cast_nullable_to_non_nullable
              as bool?,
      isError: freezed == isError
          ? _value.isError
          : isError // ignore: cast_nullable_to_non_nullable
              as bool?,
      errorMessage: freezed == errorMessage
          ? _value.errorMessage
          : errorMessage // ignore: cast_nullable_to_non_nullable
              as String?,
      metadata: freezed == metadata
          ? _value.metadata
          : metadata // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
    ) as $Val);
  }
}

/// @nodoc
abstract class _$$ChatMessageImplCopyWith<$Res>
    implements $ChatMessageCopyWith<$Res> {
  factory _$$ChatMessageImplCopyWith(
          _$ChatMessageImpl value, $Res Function(_$ChatMessageImpl) then) =
      __$$ChatMessageImplCopyWithImpl<$Res>;
  @override
  @useResult
  $Res call(
      {String id,
      MessageRole role,
      String content,
      String? thinking,
      String? sessionId,
      DateTime? timestamp,
      bool? isStreaming,
      bool? isError,
      String? errorMessage,
      Map<String, dynamic>? metadata});
}

/// @nodoc
class __$$ChatMessageImplCopyWithImpl<$Res>
    extends _$ChatMessageCopyWithImpl<$Res, _$ChatMessageImpl>
    implements _$$ChatMessageImplCopyWith<$Res> {
  __$$ChatMessageImplCopyWithImpl(
      _$ChatMessageImpl _value, $Res Function(_$ChatMessageImpl) _then)
      : super(_value, _then);

  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? id = null,
    Object? role = null,
    Object? content = null,
    Object? thinking = freezed,
    Object? sessionId = freezed,
    Object? timestamp = freezed,
    Object? isStreaming = freezed,
    Object? isError = freezed,
    Object? errorMessage = freezed,
    Object? metadata = freezed,
  }) {
    return _then(_$ChatMessageImpl(
      id: null == id
          ? _value.id
          : id // ignore: cast_nullable_to_non_nullable
              as String,
      role: null == role
          ? _value.role
          : role // ignore: cast_nullable_to_non_nullable
              as MessageRole,
      content: null == content
          ? _value.content
          : content // ignore: cast_nullable_to_non_nullable
              as String,
      thinking: freezed == thinking
          ? _value.thinking
          : thinking // ignore: cast_nullable_to_non_nullable
              as String?,
      sessionId: freezed == sessionId
          ? _value.sessionId
          : sessionId // ignore: cast_nullable_to_non_nullable
              as String?,
      timestamp: freezed == timestamp
          ? _value.timestamp
          : timestamp // ignore: cast_nullable_to_non_nullable
              as DateTime?,
      isStreaming: freezed == isStreaming
          ? _value.isStreaming
          : isStreaming // ignore: cast_nullable_to_non_nullable
              as bool?,
      isError: freezed == isError
          ? _value.isError
          : isError // ignore: cast_nullable_to_non_nullable
              as bool?,
      errorMessage: freezed == errorMessage
          ? _value.errorMessage
          : errorMessage // ignore: cast_nullable_to_non_nullable
              as String?,
      metadata: freezed == metadata
          ? _value._metadata
          : metadata // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
    ));
  }
}

/// @nodoc
@JsonSerializable()
class _$ChatMessageImpl extends _ChatMessage {
  const _$ChatMessageImpl(
      {required this.id,
      required this.role,
      required this.content,
      this.thinking,
      this.sessionId,
      this.timestamp,
      this.isStreaming,
      this.isError,
      this.errorMessage,
      final Map<String, dynamic>? metadata})
      : _metadata = metadata,
        super._();

  factory _$ChatMessageImpl.fromJson(Map<String, dynamic> json) =>
      _$$ChatMessageImplFromJson(json);

  @override
  final String id;
  @override
  final MessageRole role;
  @override
  final String content;
  @override
  final String? thinking;
  @override
  final String? sessionId;
  @override
  final DateTime? timestamp;
  @override
  final bool? isStreaming;
  @override
  final bool? isError;
  @override
  final String? errorMessage;
  final Map<String, dynamic>? _metadata;
  @override
  Map<String, dynamic>? get metadata {
    final value = _metadata;
    if (value == null) return null;
    if (_metadata is EqualUnmodifiableMapView) return _metadata;
    // ignore: implicit_dynamic_type
    return EqualUnmodifiableMapView(value);
  }

  @override
  String toString() {
    return 'ChatMessage(id: $id, role: $role, content: $content, thinking: $thinking, sessionId: $sessionId, timestamp: $timestamp, isStreaming: $isStreaming, isError: $isError, errorMessage: $errorMessage, metadata: $metadata)';
  }

  @override
  bool operator ==(Object other) {
    return identical(this, other) ||
        (other.runtimeType == runtimeType &&
            other is _$ChatMessageImpl &&
            (identical(other.id, id) || other.id == id) &&
            (identical(other.role, role) || other.role == role) &&
            (identical(other.content, content) || other.content == content) &&
            (identical(other.thinking, thinking) ||
                other.thinking == thinking) &&
            (identical(other.sessionId, sessionId) ||
                other.sessionId == sessionId) &&
            (identical(other.timestamp, timestamp) ||
                other.timestamp == timestamp) &&
            (identical(other.isStreaming, isStreaming) ||
                other.isStreaming == isStreaming) &&
            (identical(other.isError, isError) || other.isError == isError) &&
            (identical(other.errorMessage, errorMessage) ||
                other.errorMessage == errorMessage) &&
            const DeepCollectionEquality().equals(other._metadata, _metadata));
  }

  @JsonKey(ignore: true)
  @override
  int get hashCode => Object.hash(
      runtimeType,
      id,
      role,
      content,
      thinking,
      sessionId,
      timestamp,
      isStreaming,
      isError,
      errorMessage,
      const DeepCollectionEquality().hash(_metadata));

  @JsonKey(ignore: true)
  @override
  @pragma('vm:prefer-inline')
  _$$ChatMessageImplCopyWith<_$ChatMessageImpl> get copyWith =>
      __$$ChatMessageImplCopyWithImpl<_$ChatMessageImpl>(this, _$identity);

  @override
  Map<String, dynamic> toJson() {
    return _$$ChatMessageImplToJson(
      this,
    );
  }
}

abstract class _ChatMessage extends ChatMessage {
  const factory _ChatMessage(
      {required final String id,
      required final MessageRole role,
      required final String content,
      final String? thinking,
      final String? sessionId,
      final DateTime? timestamp,
      final bool? isStreaming,
      final bool? isError,
      final String? errorMessage,
      final Map<String, dynamic>? metadata}) = _$ChatMessageImpl;
  const _ChatMessage._() : super._();

  factory _ChatMessage.fromJson(Map<String, dynamic> json) =
      _$ChatMessageImpl.fromJson;

  @override
  String get id;
  @override
  MessageRole get role;
  @override
  String get content;
  @override
  String? get thinking;
  @override
  String? get sessionId;
  @override
  DateTime? get timestamp;
  @override
  bool? get isStreaming;
  @override
  bool? get isError;
  @override
  String? get errorMessage;
  @override
  Map<String, dynamic>? get metadata;
  @override
  @JsonKey(ignore: true)
  _$$ChatMessageImplCopyWith<_$ChatMessageImpl> get copyWith =>
      throw _privateConstructorUsedError;
}

StreamEvent _$StreamEventFromJson(Map<String, dynamic> json) {
  return _StreamEvent.fromJson(json);
}

/// @nodoc
mixin _$StreamEvent {
  String get type => throw _privateConstructorUsedError;
  String? get content => throw _privateConstructorUsedError;
  String? get status => throw _privateConstructorUsedError;
  String? get sessionId => throw _privateConstructorUsedError;
  bool? get done => throw _privateConstructorUsedError;
  Map<String, dynamic>? get metadata => throw _privateConstructorUsedError;

  Map<String, dynamic> toJson() => throw _privateConstructorUsedError;
  @JsonKey(ignore: true)
  $StreamEventCopyWith<StreamEvent> get copyWith =>
      throw _privateConstructorUsedError;
}

/// @nodoc
abstract class $StreamEventCopyWith<$Res> {
  factory $StreamEventCopyWith(
          StreamEvent value, $Res Function(StreamEvent) then) =
      _$StreamEventCopyWithImpl<$Res, StreamEvent>;
  @useResult
  $Res call(
      {String type,
      String? content,
      String? status,
      String? sessionId,
      bool? done,
      Map<String, dynamic>? metadata});
}

/// @nodoc
class _$StreamEventCopyWithImpl<$Res, $Val extends StreamEvent>
    implements $StreamEventCopyWith<$Res> {
  _$StreamEventCopyWithImpl(this._value, this._then);

  // ignore: unused_field
  final $Val _value;
  // ignore: unused_field
  final $Res Function($Val) _then;

  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? type = null,
    Object? content = freezed,
    Object? status = freezed,
    Object? sessionId = freezed,
    Object? done = freezed,
    Object? metadata = freezed,
  }) {
    return _then(_value.copyWith(
      type: null == type
          ? _value.type
          : type // ignore: cast_nullable_to_non_nullable
              as String,
      content: freezed == content
          ? _value.content
          : content // ignore: cast_nullable_to_non_nullable
              as String?,
      status: freezed == status
          ? _value.status
          : status // ignore: cast_nullable_to_non_nullable
              as String?,
      sessionId: freezed == sessionId
          ? _value.sessionId
          : sessionId // ignore: cast_nullable_to_non_nullable
              as String?,
      done: freezed == done
          ? _value.done
          : done // ignore: cast_nullable_to_non_nullable
              as bool?,
      metadata: freezed == metadata
          ? _value.metadata
          : metadata // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
    ) as $Val);
  }
}

/// @nodoc
abstract class _$$StreamEventImplCopyWith<$Res>
    implements $StreamEventCopyWith<$Res> {
  factory _$$StreamEventImplCopyWith(
          _$StreamEventImpl value, $Res Function(_$StreamEventImpl) then) =
      __$$StreamEventImplCopyWithImpl<$Res>;
  @override
  @useResult
  $Res call(
      {String type,
      String? content,
      String? status,
      String? sessionId,
      bool? done,
      Map<String, dynamic>? metadata});
}

/// @nodoc
class __$$StreamEventImplCopyWithImpl<$Res>
    extends _$StreamEventCopyWithImpl<$Res, _$StreamEventImpl>
    implements _$$StreamEventImplCopyWith<$Res> {
  __$$StreamEventImplCopyWithImpl(
      _$StreamEventImpl _value, $Res Function(_$StreamEventImpl) _then)
      : super(_value, _then);

  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? type = null,
    Object? content = freezed,
    Object? status = freezed,
    Object? sessionId = freezed,
    Object? done = freezed,
    Object? metadata = freezed,
  }) {
    return _then(_$StreamEventImpl(
      type: null == type
          ? _value.type
          : type // ignore: cast_nullable_to_non_nullable
              as String,
      content: freezed == content
          ? _value.content
          : content // ignore: cast_nullable_to_non_nullable
              as String?,
      status: freezed == status
          ? _value.status
          : status // ignore: cast_nullable_to_non_nullable
              as String?,
      sessionId: freezed == sessionId
          ? _value.sessionId
          : sessionId // ignore: cast_nullable_to_non_nullable
              as String?,
      done: freezed == done
          ? _value.done
          : done // ignore: cast_nullable_to_non_nullable
              as bool?,
      metadata: freezed == metadata
          ? _value._metadata
          : metadata // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
    ));
  }
}

/// @nodoc
@JsonSerializable()
class _$StreamEventImpl implements _StreamEvent {
  const _$StreamEventImpl(
      {required this.type,
      this.content,
      this.status,
      this.sessionId,
      this.done,
      final Map<String, dynamic>? metadata})
      : _metadata = metadata;

  factory _$StreamEventImpl.fromJson(Map<String, dynamic> json) =>
      _$$StreamEventImplFromJson(json);

  @override
  final String type;
  @override
  final String? content;
  @override
  final String? status;
  @override
  final String? sessionId;
  @override
  final bool? done;
  final Map<String, dynamic>? _metadata;
  @override
  Map<String, dynamic>? get metadata {
    final value = _metadata;
    if (value == null) return null;
    if (_metadata is EqualUnmodifiableMapView) return _metadata;
    // ignore: implicit_dynamic_type
    return EqualUnmodifiableMapView(value);
  }

  @override
  String toString() {
    return 'StreamEvent(type: $type, content: $content, status: $status, sessionId: $sessionId, done: $done, metadata: $metadata)';
  }

  @override
  bool operator ==(Object other) {
    return identical(this, other) ||
        (other.runtimeType == runtimeType &&
            other is _$StreamEventImpl &&
            (identical(other.type, type) || other.type == type) &&
            (identical(other.content, content) || other.content == content) &&
            (identical(other.status, status) || other.status == status) &&
            (identical(other.sessionId, sessionId) ||
                other.sessionId == sessionId) &&
            (identical(other.done, done) || other.done == done) &&
            const DeepCollectionEquality().equals(other._metadata, _metadata));
  }

  @JsonKey(ignore: true)
  @override
  int get hashCode => Object.hash(runtimeType, type, content, status, sessionId,
      done, const DeepCollectionEquality().hash(_metadata));

  @JsonKey(ignore: true)
  @override
  @pragma('vm:prefer-inline')
  _$$StreamEventImplCopyWith<_$StreamEventImpl> get copyWith =>
      __$$StreamEventImplCopyWithImpl<_$StreamEventImpl>(this, _$identity);

  @override
  Map<String, dynamic> toJson() {
    return _$$StreamEventImplToJson(
      this,
    );
  }
}

abstract class _StreamEvent implements StreamEvent {
  const factory _StreamEvent(
      {required final String type,
      final String? content,
      final String? status,
      final String? sessionId,
      final bool? done,
      final Map<String, dynamic>? metadata}) = _$StreamEventImpl;

  factory _StreamEvent.fromJson(Map<String, dynamic> json) =
      _$StreamEventImpl.fromJson;

  @override
  String get type;
  @override
  String? get content;
  @override
  String? get status;
  @override
  String? get sessionId;
  @override
  bool? get done;
  @override
  Map<String, dynamic>? get metadata;
  @override
  @JsonKey(ignore: true)
  _$$StreamEventImplCopyWith<_$StreamEventImpl> get copyWith =>
      throw _privateConstructorUsedError;
}
