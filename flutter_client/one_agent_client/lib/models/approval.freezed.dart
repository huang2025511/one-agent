// coverage:ignore-file
// GENERATED CODE - DO NOT MODIFY BY HAND
// ignore_for_file: type=lint
// ignore_for_file: unused_element, deprecated_member_use, deprecated_member_use_from_same_package, use_function_type_syntax_for_parameters, unnecessary_const, avoid_init_to_null, invalid_override_different_default_values_named, prefer_expression_function_bodies, annotate_overrides, invalid_annotation_target, unnecessary_question_mark

part of 'approval.dart';

// **************************************************************************
// FreezedGenerator
// **************************************************************************

T _$identity<T>(T value) => value;

final _privateConstructorUsedError = UnsupportedError(
    'It seems like you constructed your class using `MyClass._()`. This constructor is only meant to be used by freezed and you are not supposed to need it nor use it.\nPlease check the documentation here for more information: https://github.com/rrousselGit/freezed#adding-getters-and-methods-to-our-models');

ApprovalRequest _$ApprovalRequestFromJson(Map<String, dynamic> json) {
  return _ApprovalRequest.fromJson(json);
}

/// @nodoc
mixin _$ApprovalRequest {
  String get id => throw _privateConstructorUsedError;
  String get operation => throw _privateConstructorUsedError;
  String? get details => throw _privateConstructorUsedError;
  String? get riskLevel => throw _privateConstructorUsedError;
  DateTime? get createdAt => throw _privateConstructorUsedError;
  String? get status => throw _privateConstructorUsedError;

  Map<String, dynamic> toJson() => throw _privateConstructorUsedError;
  @JsonKey(ignore: true)
  $ApprovalRequestCopyWith<ApprovalRequest> get copyWith =>
      throw _privateConstructorUsedError;
}

/// @nodoc
abstract class $ApprovalRequestCopyWith<$Res> {
  factory $ApprovalRequestCopyWith(
          ApprovalRequest value, $Res Function(ApprovalRequest) then) =
      _$ApprovalRequestCopyWithImpl<$Res, ApprovalRequest>;
  @useResult
  $Res call(
      {String id,
      String operation,
      String? details,
      String? riskLevel,
      DateTime? createdAt,
      String? status});
}

/// @nodoc
class _$ApprovalRequestCopyWithImpl<$Res, $Val extends ApprovalRequest>
    implements $ApprovalRequestCopyWith<$Res> {
  _$ApprovalRequestCopyWithImpl(this._value, this._then);

  // ignore: unused_field
  final $Val _value;
  // ignore: unused_field
  final $Res Function($Val) _then;

  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? id = null,
    Object? operation = null,
    Object? details = freezed,
    Object? riskLevel = freezed,
    Object? createdAt = freezed,
    Object? status = freezed,
  }) {
    return _then(_value.copyWith(
      id: null == id
          ? _value.id
          : id // ignore: cast_nullable_to_non_nullable
              as String,
      operation: null == operation
          ? _value.operation
          : operation // ignore: cast_nullable_to_non_nullable
              as String,
      details: freezed == details
          ? _value.details
          : details // ignore: cast_nullable_to_non_nullable
              as String?,
      riskLevel: freezed == riskLevel
          ? _value.riskLevel
          : riskLevel // ignore: cast_nullable_to_non_nullable
              as String?,
      createdAt: freezed == createdAt
          ? _value.createdAt
          : createdAt // ignore: cast_nullable_to_non_nullable
              as DateTime?,
      status: freezed == status
          ? _value.status
          : status // ignore: cast_nullable_to_non_nullable
              as String?,
    ) as $Val);
  }
}

/// @nodoc
abstract class _$$ApprovalRequestImplCopyWith<$Res>
    implements $ApprovalRequestCopyWith<$Res> {
  factory _$$ApprovalRequestImplCopyWith(_$ApprovalRequestImpl value,
          $Res Function(_$ApprovalRequestImpl) then) =
      __$$ApprovalRequestImplCopyWithImpl<$Res>;
  @override
  @useResult
  $Res call(
      {String id,
      String operation,
      String? details,
      String? riskLevel,
      DateTime? createdAt,
      String? status});
}

/// @nodoc
class __$$ApprovalRequestImplCopyWithImpl<$Res>
    extends _$ApprovalRequestCopyWithImpl<$Res, _$ApprovalRequestImpl>
    implements _$$ApprovalRequestImplCopyWith<$Res> {
  __$$ApprovalRequestImplCopyWithImpl(
      _$ApprovalRequestImpl _value, $Res Function(_$ApprovalRequestImpl) _then)
      : super(_value, _then);

  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? id = null,
    Object? operation = null,
    Object? details = freezed,
    Object? riskLevel = freezed,
    Object? createdAt = freezed,
    Object? status = freezed,
  }) {
    return _then(_$ApprovalRequestImpl(
      id: null == id
          ? _value.id
          : id // ignore: cast_nullable_to_non_nullable
              as String,
      operation: null == operation
          ? _value.operation
          : operation // ignore: cast_nullable_to_non_nullable
              as String,
      details: freezed == details
          ? _value.details
          : details // ignore: cast_nullable_to_non_nullable
              as String?,
      riskLevel: freezed == riskLevel
          ? _value.riskLevel
          : riskLevel // ignore: cast_nullable_to_non_nullable
              as String?,
      createdAt: freezed == createdAt
          ? _value.createdAt
          : createdAt // ignore: cast_nullable_to_non_nullable
              as DateTime?,
      status: freezed == status
          ? _value.status
          : status // ignore: cast_nullable_to_non_nullable
              as String?,
    ));
  }
}

/// @nodoc
@JsonSerializable()
class _$ApprovalRequestImpl extends _ApprovalRequest {
  const _$ApprovalRequestImpl(
      {required this.id,
      required this.operation,
      this.details,
      this.riskLevel,
      this.createdAt,
      this.status})
      : super._();

  factory _$ApprovalRequestImpl.fromJson(Map<String, dynamic> json) =>
      _$$ApprovalRequestImplFromJson(json);

  @override
  final String id;
  @override
  final String operation;
  @override
  final String? details;
  @override
  final String? riskLevel;
  @override
  final DateTime? createdAt;
  @override
  final String? status;

  @override
  String toString() {
    return 'ApprovalRequest(id: $id, operation: $operation, details: $details, riskLevel: $riskLevel, createdAt: $createdAt, status: $status)';
  }

  @override
  bool operator ==(Object other) {
    return identical(this, other) ||
        (other.runtimeType == runtimeType &&
            other is _$ApprovalRequestImpl &&
            (identical(other.id, id) || other.id == id) &&
            (identical(other.operation, operation) ||
                other.operation == operation) &&
            (identical(other.details, details) || other.details == details) &&
            (identical(other.riskLevel, riskLevel) ||
                other.riskLevel == riskLevel) &&
            (identical(other.createdAt, createdAt) ||
                other.createdAt == createdAt) &&
            (identical(other.status, status) || other.status == status));
  }

  @JsonKey(ignore: true)
  @override
  int get hashCode => Object.hash(
      runtimeType, id, operation, details, riskLevel, createdAt, status);

  @JsonKey(ignore: true)
  @override
  @pragma('vm:prefer-inline')
  _$$ApprovalRequestImplCopyWith<_$ApprovalRequestImpl> get copyWith =>
      __$$ApprovalRequestImplCopyWithImpl<_$ApprovalRequestImpl>(
          this, _$identity);

  @override
  Map<String, dynamic> toJson() {
    return _$$ApprovalRequestImplToJson(
      this,
    );
  }
}

abstract class _ApprovalRequest extends ApprovalRequest {
  const factory _ApprovalRequest(
      {required final String id,
      required final String operation,
      final String? details,
      final String? riskLevel,
      final DateTime? createdAt,
      final String? status}) = _$ApprovalRequestImpl;
  const _ApprovalRequest._() : super._();

  factory _ApprovalRequest.fromJson(Map<String, dynamic> json) =
      _$ApprovalRequestImpl.fromJson;

  @override
  String get id;
  @override
  String get operation;
  @override
  String? get details;
  @override
  String? get riskLevel;
  @override
  DateTime? get createdAt;
  @override
  String? get status;
  @override
  @JsonKey(ignore: true)
  _$$ApprovalRequestImplCopyWith<_$ApprovalRequestImpl> get copyWith =>
      throw _privateConstructorUsedError;
}
