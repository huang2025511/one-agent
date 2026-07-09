// coverage:ignore-file
// GENERATED CODE - DO NOT MODIFY BY HAND
// ignore_for_file: type=lint
// ignore_for_file: unused_element, deprecated_member_use, deprecated_member_use_from_same_package, use_function_type_syntax_for_parameters, unnecessary_const, avoid_init_to_null, invalid_override_different_default_values_named, prefer_expression_function_bodies, annotate_overrides, invalid_annotation_target, unnecessary_question_mark

part of 'memory.dart';

// **************************************************************************
// FreezedGenerator
// **************************************************************************

T _$identity<T>(T value) => value;

final _privateConstructorUsedError = UnsupportedError(
    'It seems like you constructed your class using `MyClass._()`. This constructor is only meant to be used by freezed and you are not supposed to need it nor use it.\nPlease check the documentation here for more information: https://github.com/rrousselGit/freezed#adding-getters-and-methods-to-our-models');

Memory _$MemoryFromJson(Map<String, dynamic> json) {
  return _Memory.fromJson(json);
}

/// @nodoc
mixin _$Memory {
  int get id => throw _privateConstructorUsedError;
  String get text => throw _privateConstructorUsedError;
  String? get source => throw _privateConstructorUsedError;
  String? get tags => throw _privateConstructorUsedError;
  DateTime? get createdAt => throw _privateConstructorUsedError;
  double? get relevance => throw _privateConstructorUsedError;

  Map<String, dynamic> toJson() => throw _privateConstructorUsedError;
  @JsonKey(ignore: true)
  $MemoryCopyWith<Memory> get copyWith => throw _privateConstructorUsedError;
}

/// @nodoc
abstract class $MemoryCopyWith<$Res> {
  factory $MemoryCopyWith(Memory value, $Res Function(Memory) then) =
      _$MemoryCopyWithImpl<$Res, Memory>;
  @useResult
  $Res call(
      {int id,
      String text,
      String? source,
      String? tags,
      DateTime? createdAt,
      double? relevance});
}

/// @nodoc
class _$MemoryCopyWithImpl<$Res, $Val extends Memory>
    implements $MemoryCopyWith<$Res> {
  _$MemoryCopyWithImpl(this._value, this._then);

  // ignore: unused_field
  final $Val _value;
  // ignore: unused_field
  final $Res Function($Val) _then;

  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? id = null,
    Object? text = null,
    Object? source = freezed,
    Object? tags = freezed,
    Object? createdAt = freezed,
    Object? relevance = freezed,
  }) {
    return _then(_value.copyWith(
      id: null == id
          ? _value.id
          : id // ignore: cast_nullable_to_non_nullable
              as int,
      text: null == text
          ? _value.text
          : text // ignore: cast_nullable_to_non_nullable
              as String,
      source: freezed == source
          ? _value.source
          : source // ignore: cast_nullable_to_non_nullable
              as String?,
      tags: freezed == tags
          ? _value.tags
          : tags // ignore: cast_nullable_to_non_nullable
              as String?,
      createdAt: freezed == createdAt
          ? _value.createdAt
          : createdAt // ignore: cast_nullable_to_non_nullable
              as DateTime?,
      relevance: freezed == relevance
          ? _value.relevance
          : relevance // ignore: cast_nullable_to_non_nullable
              as double?,
    ) as $Val);
  }
}

/// @nodoc
abstract class _$$MemoryImplCopyWith<$Res> implements $MemoryCopyWith<$Res> {
  factory _$$MemoryImplCopyWith(
          _$MemoryImpl value, $Res Function(_$MemoryImpl) then) =
      __$$MemoryImplCopyWithImpl<$Res>;
  @override
  @useResult
  $Res call(
      {int id,
      String text,
      String? source,
      String? tags,
      DateTime? createdAt,
      double? relevance});
}

/// @nodoc
class __$$MemoryImplCopyWithImpl<$Res>
    extends _$MemoryCopyWithImpl<$Res, _$MemoryImpl>
    implements _$$MemoryImplCopyWith<$Res> {
  __$$MemoryImplCopyWithImpl(
      _$MemoryImpl _value, $Res Function(_$MemoryImpl) _then)
      : super(_value, _then);

  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? id = null,
    Object? text = null,
    Object? source = freezed,
    Object? tags = freezed,
    Object? createdAt = freezed,
    Object? relevance = freezed,
  }) {
    return _then(_$MemoryImpl(
      id: null == id
          ? _value.id
          : id // ignore: cast_nullable_to_non_nullable
              as int,
      text: null == text
          ? _value.text
          : text // ignore: cast_nullable_to_non_nullable
              as String,
      source: freezed == source
          ? _value.source
          : source // ignore: cast_nullable_to_non_nullable
              as String?,
      tags: freezed == tags
          ? _value.tags
          : tags // ignore: cast_nullable_to_non_nullable
              as String?,
      createdAt: freezed == createdAt
          ? _value.createdAt
          : createdAt // ignore: cast_nullable_to_non_nullable
              as DateTime?,
      relevance: freezed == relevance
          ? _value.relevance
          : relevance // ignore: cast_nullable_to_non_nullable
              as double?,
    ));
  }
}

/// @nodoc
@JsonSerializable()
class _$MemoryImpl extends _Memory {
  const _$MemoryImpl(
      {required this.id,
      required this.text,
      this.source,
      this.tags,
      this.createdAt,
      this.relevance})
      : super._();

  factory _$MemoryImpl.fromJson(Map<String, dynamic> json) =>
      _$$MemoryImplFromJson(json);

  @override
  final int id;
  @override
  final String text;
  @override
  final String? source;
  @override
  final String? tags;
  @override
  final DateTime? createdAt;
  @override
  final double? relevance;

  @override
  String toString() {
    return 'Memory(id: $id, text: $text, source: $source, tags: $tags, createdAt: $createdAt, relevance: $relevance)';
  }

  @override
  bool operator ==(Object other) {
    return identical(this, other) ||
        (other.runtimeType == runtimeType &&
            other is _$MemoryImpl &&
            (identical(other.id, id) || other.id == id) &&
            (identical(other.text, text) || other.text == text) &&
            (identical(other.source, source) || other.source == source) &&
            (identical(other.tags, tags) || other.tags == tags) &&
            (identical(other.createdAt, createdAt) ||
                other.createdAt == createdAt) &&
            (identical(other.relevance, relevance) ||
                other.relevance == relevance));
  }

  @JsonKey(ignore: true)
  @override
  int get hashCode =>
      Object.hash(runtimeType, id, text, source, tags, createdAt, relevance);

  @JsonKey(ignore: true)
  @override
  @pragma('vm:prefer-inline')
  _$$MemoryImplCopyWith<_$MemoryImpl> get copyWith =>
      __$$MemoryImplCopyWithImpl<_$MemoryImpl>(this, _$identity);

  @override
  Map<String, dynamic> toJson() {
    return _$$MemoryImplToJson(
      this,
    );
  }
}

abstract class _Memory extends Memory {
  const factory _Memory(
      {required final int id,
      required final String text,
      final String? source,
      final String? tags,
      final DateTime? createdAt,
      final double? relevance}) = _$MemoryImpl;
  const _Memory._() : super._();

  factory _Memory.fromJson(Map<String, dynamic> json) = _$MemoryImpl.fromJson;

  @override
  int get id;
  @override
  String get text;
  @override
  String? get source;
  @override
  String? get tags;
  @override
  DateTime? get createdAt;
  @override
  double? get relevance;
  @override
  @JsonKey(ignore: true)
  _$$MemoryImplCopyWith<_$MemoryImpl> get copyWith =>
      throw _privateConstructorUsedError;
}

MemoryPage _$MemoryPageFromJson(Map<String, dynamic> json) {
  return _MemoryPage.fromJson(json);
}

/// @nodoc
mixin _$MemoryPage {
  List<Memory> get items => throw _privateConstructorUsedError;
  int get total => throw _privateConstructorUsedError;
  int get page => throw _privateConstructorUsedError;
  int get pageSize => throw _privateConstructorUsedError;

  Map<String, dynamic> toJson() => throw _privateConstructorUsedError;
  @JsonKey(ignore: true)
  $MemoryPageCopyWith<MemoryPage> get copyWith =>
      throw _privateConstructorUsedError;
}

/// @nodoc
abstract class $MemoryPageCopyWith<$Res> {
  factory $MemoryPageCopyWith(
          MemoryPage value, $Res Function(MemoryPage) then) =
      _$MemoryPageCopyWithImpl<$Res, MemoryPage>;
  @useResult
  $Res call({List<Memory> items, int total, int page, int pageSize});
}

/// @nodoc
class _$MemoryPageCopyWithImpl<$Res, $Val extends MemoryPage>
    implements $MemoryPageCopyWith<$Res> {
  _$MemoryPageCopyWithImpl(this._value, this._then);

  // ignore: unused_field
  final $Val _value;
  // ignore: unused_field
  final $Res Function($Val) _then;

  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? items = null,
    Object? total = null,
    Object? page = null,
    Object? pageSize = null,
  }) {
    return _then(_value.copyWith(
      items: null == items
          ? _value.items
          : items // ignore: cast_nullable_to_non_nullable
              as List<Memory>,
      total: null == total
          ? _value.total
          : total // ignore: cast_nullable_to_non_nullable
              as int,
      page: null == page
          ? _value.page
          : page // ignore: cast_nullable_to_non_nullable
              as int,
      pageSize: null == pageSize
          ? _value.pageSize
          : pageSize // ignore: cast_nullable_to_non_nullable
              as int,
    ) as $Val);
  }
}

/// @nodoc
abstract class _$$MemoryPageImplCopyWith<$Res>
    implements $MemoryPageCopyWith<$Res> {
  factory _$$MemoryPageImplCopyWith(
          _$MemoryPageImpl value, $Res Function(_$MemoryPageImpl) then) =
      __$$MemoryPageImplCopyWithImpl<$Res>;
  @override
  @useResult
  $Res call({List<Memory> items, int total, int page, int pageSize});
}

/// @nodoc
class __$$MemoryPageImplCopyWithImpl<$Res>
    extends _$MemoryPageCopyWithImpl<$Res, _$MemoryPageImpl>
    implements _$$MemoryPageImplCopyWith<$Res> {
  __$$MemoryPageImplCopyWithImpl(
      _$MemoryPageImpl _value, $Res Function(_$MemoryPageImpl) _then)
      : super(_value, _then);

  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? items = null,
    Object? total = null,
    Object? page = null,
    Object? pageSize = null,
  }) {
    return _then(_$MemoryPageImpl(
      items: null == items
          ? _value._items
          : items // ignore: cast_nullable_to_non_nullable
              as List<Memory>,
      total: null == total
          ? _value.total
          : total // ignore: cast_nullable_to_non_nullable
              as int,
      page: null == page
          ? _value.page
          : page // ignore: cast_nullable_to_non_nullable
              as int,
      pageSize: null == pageSize
          ? _value.pageSize
          : pageSize // ignore: cast_nullable_to_non_nullable
              as int,
    ));
  }
}

/// @nodoc
@JsonSerializable()
class _$MemoryPageImpl implements _MemoryPage {
  const _$MemoryPageImpl(
      {required final List<Memory> items,
      required this.total,
      required this.page,
      required this.pageSize})
      : _items = items;

  factory _$MemoryPageImpl.fromJson(Map<String, dynamic> json) =>
      _$$MemoryPageImplFromJson(json);

  final List<Memory> _items;
  @override
  List<Memory> get items {
    if (_items is EqualUnmodifiableListView) return _items;
    // ignore: implicit_dynamic_type
    return EqualUnmodifiableListView(_items);
  }

  @override
  final int total;
  @override
  final int page;
  @override
  final int pageSize;

  @override
  String toString() {
    return 'MemoryPage(items: $items, total: $total, page: $page, pageSize: $pageSize)';
  }

  @override
  bool operator ==(Object other) {
    return identical(this, other) ||
        (other.runtimeType == runtimeType &&
            other is _$MemoryPageImpl &&
            const DeepCollectionEquality().equals(other._items, _items) &&
            (identical(other.total, total) || other.total == total) &&
            (identical(other.page, page) || other.page == page) &&
            (identical(other.pageSize, pageSize) ||
                other.pageSize == pageSize));
  }

  @JsonKey(ignore: true)
  @override
  int get hashCode => Object.hash(runtimeType,
      const DeepCollectionEquality().hash(_items), total, page, pageSize);

  @JsonKey(ignore: true)
  @override
  @pragma('vm:prefer-inline')
  _$$MemoryPageImplCopyWith<_$MemoryPageImpl> get copyWith =>
      __$$MemoryPageImplCopyWithImpl<_$MemoryPageImpl>(this, _$identity);

  @override
  Map<String, dynamic> toJson() {
    return _$$MemoryPageImplToJson(
      this,
    );
  }
}

abstract class _MemoryPage implements MemoryPage {
  const factory _MemoryPage(
      {required final List<Memory> items,
      required final int total,
      required final int page,
      required final int pageSize}) = _$MemoryPageImpl;

  factory _MemoryPage.fromJson(Map<String, dynamic> json) =
      _$MemoryPageImpl.fromJson;

  @override
  List<Memory> get items;
  @override
  int get total;
  @override
  int get page;
  @override
  int get pageSize;
  @override
  @JsonKey(ignore: true)
  _$$MemoryPageImplCopyWith<_$MemoryPageImpl> get copyWith =>
      throw _privateConstructorUsedError;
}
