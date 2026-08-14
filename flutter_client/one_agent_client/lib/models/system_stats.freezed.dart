// coverage:ignore-file
// GENERATED CODE - DO NOT MODIFY BY HAND
// ignore_for_file: type=lint
// ignore_for_file: unused_element, deprecated_member_use, deprecated_member_use_from_same_package, use_function_type_syntax_for_parameters, unnecessary_const, avoid_init_to_null, invalid_override_different_default_values_named, prefer_expression_function_bodies, annotate_overrides, invalid_annotation_target, unnecessary_question_mark

part of 'system_stats.dart';

// **************************************************************************
// FreezedGenerator
// **************************************************************************

T _$identity<T>(T value) => value;

final _privateConstructorUsedError = UnsupportedError(
    'It seems like you constructed your class using `MyClass._()`. This constructor is only meant to be used by freezed and you are not supposed to need it nor use it.\nPlease check the documentation here for more information: https://github.com/rrousselGit/freezed#adding-getters-and-methods-to-our-models');

SystemStats _$SystemStatsFromJson(Map<String, dynamic> json) {
  return _SystemStats.fromJson(json);
}

/// @nodoc
mixin _$SystemStats {
  @JsonKey(name: 'uptime_seconds')
  int? get uptimeSeconds => throw _privateConstructorUsedError;
  @JsonKey(name: 'bus_metrics')
  Map<String, dynamic>? get busMetrics => throw _privateConstructorUsedError;
  @JsonKey(name: 'llm_stats')
  Map<String, dynamic>? get llmStats => throw _privateConstructorUsedError;
  @JsonKey(name: 'memory_stats')
  Map<String, dynamic>? get memoryStats => throw _privateConstructorUsedError;
  @JsonKey(name: 'skills_count')
  int? get skillsCount => throw _privateConstructorUsedError;
  Map<String, dynamic>? get sessions => throw _privateConstructorUsedError;
  Map<String, dynamic>? get messages => throw _privateConstructorUsedError;
  @JsonKey(name: 'knowledge_graph')
  Map<String, dynamic>? get knowledgeGraph =>
      throw _privateConstructorUsedError;

  /// Serializes this SystemStats to a JSON map.
  Map<String, dynamic> toJson() => throw _privateConstructorUsedError;

  /// Create a copy of SystemStats
  /// with the given fields replaced by the non-null parameter values.
  @JsonKey(includeFromJson: false, includeToJson: false)
  $SystemStatsCopyWith<SystemStats> get copyWith =>
      throw _privateConstructorUsedError;
}

/// @nodoc
abstract class $SystemStatsCopyWith<$Res> {
  factory $SystemStatsCopyWith(
          SystemStats value, $Res Function(SystemStats) then) =
      _$SystemStatsCopyWithImpl<$Res, SystemStats>;
  @useResult
  $Res call(
      {@JsonKey(name: 'uptime_seconds') int? uptimeSeconds,
      @JsonKey(name: 'bus_metrics') Map<String, dynamic>? busMetrics,
      @JsonKey(name: 'llm_stats') Map<String, dynamic>? llmStats,
      @JsonKey(name: 'memory_stats') Map<String, dynamic>? memoryStats,
      @JsonKey(name: 'skills_count') int? skillsCount,
      Map<String, dynamic>? sessions,
      Map<String, dynamic>? messages,
      @JsonKey(name: 'knowledge_graph') Map<String, dynamic>? knowledgeGraph});
}

/// @nodoc
class _$SystemStatsCopyWithImpl<$Res, $Val extends SystemStats>
    implements $SystemStatsCopyWith<$Res> {
  _$SystemStatsCopyWithImpl(this._value, this._then);

  // ignore: unused_field
  final $Val _value;
  // ignore: unused_field
  final $Res Function($Val) _then;

  /// Create a copy of SystemStats
  /// with the given fields replaced by the non-null parameter values.
  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? uptimeSeconds = freezed,
    Object? busMetrics = freezed,
    Object? llmStats = freezed,
    Object? memoryStats = freezed,
    Object? skillsCount = freezed,
    Object? sessions = freezed,
    Object? messages = freezed,
    Object? knowledgeGraph = freezed,
  }) {
    return _then(_value.copyWith(
      uptimeSeconds: freezed == uptimeSeconds
          ? _value.uptimeSeconds
          : uptimeSeconds // ignore: cast_nullable_to_non_nullable
              as int?,
      busMetrics: freezed == busMetrics
          ? _value.busMetrics
          : busMetrics // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      llmStats: freezed == llmStats
          ? _value.llmStats
          : llmStats // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      memoryStats: freezed == memoryStats
          ? _value.memoryStats
          : memoryStats // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      skillsCount: freezed == skillsCount
          ? _value.skillsCount
          : skillsCount // ignore: cast_nullable_to_non_nullable
              as int?,
      sessions: freezed == sessions
          ? _value.sessions
          : sessions // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      messages: freezed == messages
          ? _value.messages
          : messages // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      knowledgeGraph: freezed == knowledgeGraph
          ? _value.knowledgeGraph
          : knowledgeGraph // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
    ) as $Val);
  }
}

/// @nodoc
abstract class _$$SystemStatsImplCopyWith<$Res>
    implements $SystemStatsCopyWith<$Res> {
  factory _$$SystemStatsImplCopyWith(
          _$SystemStatsImpl value, $Res Function(_$SystemStatsImpl) then) =
      __$$SystemStatsImplCopyWithImpl<$Res>;
  @override
  @useResult
  $Res call(
      {@JsonKey(name: 'uptime_seconds') int? uptimeSeconds,
      @JsonKey(name: 'bus_metrics') Map<String, dynamic>? busMetrics,
      @JsonKey(name: 'llm_stats') Map<String, dynamic>? llmStats,
      @JsonKey(name: 'memory_stats') Map<String, dynamic>? memoryStats,
      @JsonKey(name: 'skills_count') int? skillsCount,
      Map<String, dynamic>? sessions,
      Map<String, dynamic>? messages,
      @JsonKey(name: 'knowledge_graph') Map<String, dynamic>? knowledgeGraph});
}

/// @nodoc
class __$$SystemStatsImplCopyWithImpl<$Res>
    extends _$SystemStatsCopyWithImpl<$Res, _$SystemStatsImpl>
    implements _$$SystemStatsImplCopyWith<$Res> {
  __$$SystemStatsImplCopyWithImpl(
      _$SystemStatsImpl _value, $Res Function(_$SystemStatsImpl) _then)
      : super(_value, _then);

  /// Create a copy of SystemStats
  /// with the given fields replaced by the non-null parameter values.
  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? uptimeSeconds = freezed,
    Object? busMetrics = freezed,
    Object? llmStats = freezed,
    Object? memoryStats = freezed,
    Object? skillsCount = freezed,
    Object? sessions = freezed,
    Object? messages = freezed,
    Object? knowledgeGraph = freezed,
  }) {
    return _then(_$SystemStatsImpl(
      uptimeSeconds: freezed == uptimeSeconds
          ? _value.uptimeSeconds
          : uptimeSeconds // ignore: cast_nullable_to_non_nullable
              as int?,
      busMetrics: freezed == busMetrics
          ? _value._busMetrics
          : busMetrics // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      llmStats: freezed == llmStats
          ? _value._llmStats
          : llmStats // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      memoryStats: freezed == memoryStats
          ? _value._memoryStats
          : memoryStats // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      skillsCount: freezed == skillsCount
          ? _value.skillsCount
          : skillsCount // ignore: cast_nullable_to_non_nullable
              as int?,
      sessions: freezed == sessions
          ? _value._sessions
          : sessions // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      messages: freezed == messages
          ? _value._messages
          : messages // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      knowledgeGraph: freezed == knowledgeGraph
          ? _value._knowledgeGraph
          : knowledgeGraph // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
    ));
  }
}

/// @nodoc
@JsonSerializable()
class _$SystemStatsImpl implements _SystemStats {
  const _$SystemStatsImpl(
      {@JsonKey(name: 'uptime_seconds') this.uptimeSeconds,
      @JsonKey(name: 'bus_metrics') final Map<String, dynamic>? busMetrics,
      @JsonKey(name: 'llm_stats') final Map<String, dynamic>? llmStats,
      @JsonKey(name: 'memory_stats') final Map<String, dynamic>? memoryStats,
      @JsonKey(name: 'skills_count') this.skillsCount,
      final Map<String, dynamic>? sessions,
      final Map<String, dynamic>? messages,
      @JsonKey(name: 'knowledge_graph')
      final Map<String, dynamic>? knowledgeGraph})
      : _busMetrics = busMetrics,
        _llmStats = llmStats,
        _memoryStats = memoryStats,
        _sessions = sessions,
        _messages = messages,
        _knowledgeGraph = knowledgeGraph;

  factory _$SystemStatsImpl.fromJson(Map<String, dynamic> json) =>
      _$$SystemStatsImplFromJson(json);

  @override
  @JsonKey(name: 'uptime_seconds')
  final int? uptimeSeconds;
  final Map<String, dynamic>? _busMetrics;
  @override
  @JsonKey(name: 'bus_metrics')
  Map<String, dynamic>? get busMetrics {
    final value = _busMetrics;
    if (value == null) return null;
    if (_busMetrics is EqualUnmodifiableMapView) return _busMetrics;
    // ignore: implicit_dynamic_type
    return EqualUnmodifiableMapView(value);
  }

  final Map<String, dynamic>? _llmStats;
  @override
  @JsonKey(name: 'llm_stats')
  Map<String, dynamic>? get llmStats {
    final value = _llmStats;
    if (value == null) return null;
    if (_llmStats is EqualUnmodifiableMapView) return _llmStats;
    // ignore: implicit_dynamic_type
    return EqualUnmodifiableMapView(value);
  }

  final Map<String, dynamic>? _memoryStats;
  @override
  @JsonKey(name: 'memory_stats')
  Map<String, dynamic>? get memoryStats {
    final value = _memoryStats;
    if (value == null) return null;
    if (_memoryStats is EqualUnmodifiableMapView) return _memoryStats;
    // ignore: implicit_dynamic_type
    return EqualUnmodifiableMapView(value);
  }

  @override
  @JsonKey(name: 'skills_count')
  final int? skillsCount;
  final Map<String, dynamic>? _sessions;
  @override
  Map<String, dynamic>? get sessions {
    final value = _sessions;
    if (value == null) return null;
    if (_sessions is EqualUnmodifiableMapView) return _sessions;
    // ignore: implicit_dynamic_type
    return EqualUnmodifiableMapView(value);
  }

  final Map<String, dynamic>? _messages;
  @override
  Map<String, dynamic>? get messages {
    final value = _messages;
    if (value == null) return null;
    if (_messages is EqualUnmodifiableMapView) return _messages;
    // ignore: implicit_dynamic_type
    return EqualUnmodifiableMapView(value);
  }

  final Map<String, dynamic>? _knowledgeGraph;
  @override
  @JsonKey(name: 'knowledge_graph')
  Map<String, dynamic>? get knowledgeGraph {
    final value = _knowledgeGraph;
    if (value == null) return null;
    if (_knowledgeGraph is EqualUnmodifiableMapView) return _knowledgeGraph;
    // ignore: implicit_dynamic_type
    return EqualUnmodifiableMapView(value);
  }

  @override
  String toString() {
    return 'SystemStats(uptimeSeconds: $uptimeSeconds, busMetrics: $busMetrics, llmStats: $llmStats, memoryStats: $memoryStats, skillsCount: $skillsCount, sessions: $sessions, messages: $messages, knowledgeGraph: $knowledgeGraph)';
  }

  @override
  bool operator ==(Object other) {
    return identical(this, other) ||
        (other.runtimeType == runtimeType &&
            other is _$SystemStatsImpl &&
            (identical(other.uptimeSeconds, uptimeSeconds) ||
                other.uptimeSeconds == uptimeSeconds) &&
            const DeepCollectionEquality()
                .equals(other._busMetrics, _busMetrics) &&
            const DeepCollectionEquality().equals(other._llmStats, _llmStats) &&
            const DeepCollectionEquality()
                .equals(other._memoryStats, _memoryStats) &&
            (identical(other.skillsCount, skillsCount) ||
                other.skillsCount == skillsCount) &&
            const DeepCollectionEquality().equals(other._sessions, _sessions) &&
            const DeepCollectionEquality().equals(other._messages, _messages) &&
            const DeepCollectionEquality()
                .equals(other._knowledgeGraph, _knowledgeGraph));
  }

  @JsonKey(includeFromJson: false, includeToJson: false)
  @override
  int get hashCode => Object.hash(
      runtimeType,
      uptimeSeconds,
      const DeepCollectionEquality().hash(_busMetrics),
      const DeepCollectionEquality().hash(_llmStats),
      const DeepCollectionEquality().hash(_memoryStats),
      skillsCount,
      const DeepCollectionEquality().hash(_sessions),
      const DeepCollectionEquality().hash(_messages),
      const DeepCollectionEquality().hash(_knowledgeGraph));

  /// Create a copy of SystemStats
  /// with the given fields replaced by the non-null parameter values.
  @JsonKey(includeFromJson: false, includeToJson: false)
  @override
  @pragma('vm:prefer-inline')
  _$$SystemStatsImplCopyWith<_$SystemStatsImpl> get copyWith =>
      __$$SystemStatsImplCopyWithImpl<_$SystemStatsImpl>(this, _$identity);

  @override
  Map<String, dynamic> toJson() {
    return _$$SystemStatsImplToJson(
      this,
    );
  }
}

abstract class _SystemStats implements SystemStats {
  const factory _SystemStats(
      {@JsonKey(name: 'uptime_seconds') final int? uptimeSeconds,
      @JsonKey(name: 'bus_metrics') final Map<String, dynamic>? busMetrics,
      @JsonKey(name: 'llm_stats') final Map<String, dynamic>? llmStats,
      @JsonKey(name: 'memory_stats') final Map<String, dynamic>? memoryStats,
      @JsonKey(name: 'skills_count') final int? skillsCount,
      final Map<String, dynamic>? sessions,
      final Map<String, dynamic>? messages,
      @JsonKey(name: 'knowledge_graph')
      final Map<String, dynamic>? knowledgeGraph}) = _$SystemStatsImpl;

  factory _SystemStats.fromJson(Map<String, dynamic> json) =
      _$SystemStatsImpl.fromJson;

  @override
  @JsonKey(name: 'uptime_seconds')
  int? get uptimeSeconds;
  @override
  @JsonKey(name: 'bus_metrics')
  Map<String, dynamic>? get busMetrics;
  @override
  @JsonKey(name: 'llm_stats')
  Map<String, dynamic>? get llmStats;
  @override
  @JsonKey(name: 'memory_stats')
  Map<String, dynamic>? get memoryStats;
  @override
  @JsonKey(name: 'skills_count')
  int? get skillsCount;
  @override
  Map<String, dynamic>? get sessions;
  @override
  Map<String, dynamic>? get messages;
  @override
  @JsonKey(name: 'knowledge_graph')
  Map<String, dynamic>? get knowledgeGraph;

  /// Create a copy of SystemStats
  /// with the given fields replaced by the non-null parameter values.
  @override
  @JsonKey(includeFromJson: false, includeToJson: false)
  _$$SystemStatsImplCopyWith<_$SystemStatsImpl> get copyWith =>
      throw _privateConstructorUsedError;
}

SystemHealth _$SystemHealthFromJson(Map<String, dynamic> json) {
  return _SystemHealth.fromJson(json);
}

/// @nodoc
mixin _$SystemHealth {
  String get status => throw _privateConstructorUsedError;
  int? get uptime => throw _privateConstructorUsedError;
  String? get version => throw _privateConstructorUsedError;
  Map<String, dynamic>? get components => throw _privateConstructorUsedError;

  /// Serializes this SystemHealth to a JSON map.
  Map<String, dynamic> toJson() => throw _privateConstructorUsedError;

  /// Create a copy of SystemHealth
  /// with the given fields replaced by the non-null parameter values.
  @JsonKey(includeFromJson: false, includeToJson: false)
  $SystemHealthCopyWith<SystemHealth> get copyWith =>
      throw _privateConstructorUsedError;
}

/// @nodoc
abstract class $SystemHealthCopyWith<$Res> {
  factory $SystemHealthCopyWith(
          SystemHealth value, $Res Function(SystemHealth) then) =
      _$SystemHealthCopyWithImpl<$Res, SystemHealth>;
  @useResult
  $Res call(
      {String status,
      int? uptime,
      String? version,
      Map<String, dynamic>? components});
}

/// @nodoc
class _$SystemHealthCopyWithImpl<$Res, $Val extends SystemHealth>
    implements $SystemHealthCopyWith<$Res> {
  _$SystemHealthCopyWithImpl(this._value, this._then);

  // ignore: unused_field
  final $Val _value;
  // ignore: unused_field
  final $Res Function($Val) _then;

  /// Create a copy of SystemHealth
  /// with the given fields replaced by the non-null parameter values.
  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? status = null,
    Object? uptime = freezed,
    Object? version = freezed,
    Object? components = freezed,
  }) {
    return _then(_value.copyWith(
      status: null == status
          ? _value.status
          : status // ignore: cast_nullable_to_non_nullable
              as String,
      uptime: freezed == uptime
          ? _value.uptime
          : uptime // ignore: cast_nullable_to_non_nullable
              as int?,
      version: freezed == version
          ? _value.version
          : version // ignore: cast_nullable_to_non_nullable
              as String?,
      components: freezed == components
          ? _value.components
          : components // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
    ) as $Val);
  }
}

/// @nodoc
abstract class _$$SystemHealthImplCopyWith<$Res>
    implements $SystemHealthCopyWith<$Res> {
  factory _$$SystemHealthImplCopyWith(
          _$SystemHealthImpl value, $Res Function(_$SystemHealthImpl) then) =
      __$$SystemHealthImplCopyWithImpl<$Res>;
  @override
  @useResult
  $Res call(
      {String status,
      int? uptime,
      String? version,
      Map<String, dynamic>? components});
}

/// @nodoc
class __$$SystemHealthImplCopyWithImpl<$Res>
    extends _$SystemHealthCopyWithImpl<$Res, _$SystemHealthImpl>
    implements _$$SystemHealthImplCopyWith<$Res> {
  __$$SystemHealthImplCopyWithImpl(
      _$SystemHealthImpl _value, $Res Function(_$SystemHealthImpl) _then)
      : super(_value, _then);

  /// Create a copy of SystemHealth
  /// with the given fields replaced by the non-null parameter values.
  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? status = null,
    Object? uptime = freezed,
    Object? version = freezed,
    Object? components = freezed,
  }) {
    return _then(_$SystemHealthImpl(
      status: null == status
          ? _value.status
          : status // ignore: cast_nullable_to_non_nullable
              as String,
      uptime: freezed == uptime
          ? _value.uptime
          : uptime // ignore: cast_nullable_to_non_nullable
              as int?,
      version: freezed == version
          ? _value.version
          : version // ignore: cast_nullable_to_non_nullable
              as String?,
      components: freezed == components
          ? _value._components
          : components // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
    ));
  }
}

/// @nodoc
@JsonSerializable()
class _$SystemHealthImpl implements _SystemHealth {
  const _$SystemHealthImpl(
      {required this.status,
      this.uptime,
      this.version,
      final Map<String, dynamic>? components})
      : _components = components;

  factory _$SystemHealthImpl.fromJson(Map<String, dynamic> json) =>
      _$$SystemHealthImplFromJson(json);

  @override
  final String status;
  @override
  final int? uptime;
  @override
  final String? version;
  final Map<String, dynamic>? _components;
  @override
  Map<String, dynamic>? get components {
    final value = _components;
    if (value == null) return null;
    if (_components is EqualUnmodifiableMapView) return _components;
    // ignore: implicit_dynamic_type
    return EqualUnmodifiableMapView(value);
  }

  @override
  String toString() {
    return 'SystemHealth(status: $status, uptime: $uptime, version: $version, components: $components)';
  }

  @override
  bool operator ==(Object other) {
    return identical(this, other) ||
        (other.runtimeType == runtimeType &&
            other is _$SystemHealthImpl &&
            (identical(other.status, status) || other.status == status) &&
            (identical(other.uptime, uptime) || other.uptime == uptime) &&
            (identical(other.version, version) || other.version == version) &&
            const DeepCollectionEquality()
                .equals(other._components, _components));
  }

  @JsonKey(includeFromJson: false, includeToJson: false)
  @override
  int get hashCode => Object.hash(runtimeType, status, uptime, version,
      const DeepCollectionEquality().hash(_components));

  /// Create a copy of SystemHealth
  /// with the given fields replaced by the non-null parameter values.
  @JsonKey(includeFromJson: false, includeToJson: false)
  @override
  @pragma('vm:prefer-inline')
  _$$SystemHealthImplCopyWith<_$SystemHealthImpl> get copyWith =>
      __$$SystemHealthImplCopyWithImpl<_$SystemHealthImpl>(this, _$identity);

  @override
  Map<String, dynamic> toJson() {
    return _$$SystemHealthImplToJson(
      this,
    );
  }
}

abstract class _SystemHealth implements SystemHealth {
  const factory _SystemHealth(
      {required final String status,
      final int? uptime,
      final String? version,
      final Map<String, dynamic>? components}) = _$SystemHealthImpl;

  factory _SystemHealth.fromJson(Map<String, dynamic> json) =
      _$SystemHealthImpl.fromJson;

  @override
  String get status;
  @override
  int? get uptime;
  @override
  String? get version;
  @override
  Map<String, dynamic>? get components;

  /// Create a copy of SystemHealth
  /// with the given fields replaced by the non-null parameter values.
  @override
  @JsonKey(includeFromJson: false, includeToJson: false)
  _$$SystemHealthImplCopyWith<_$SystemHealthImpl> get copyWith =>
      throw _privateConstructorUsedError;
}

CostStats _$CostStatsFromJson(Map<String, dynamic> json) {
  return _CostStats.fromJson(json);
}

/// @nodoc
mixin _$CostStats {
  Map<String, dynamic>? get daily => throw _privateConstructorUsedError;
  Map<String, dynamic>? get monthly => throw _privateConstructorUsedError;
  @JsonKey(name: 'by_provider')
  Map<String, dynamic>? get byProvider => throw _privateConstructorUsedError;
  @JsonKey(name: 'by_model')
  Map<String, dynamic>? get byModel => throw _privateConstructorUsedError;
  @JsonKey(name: 'total_tokens')
  int? get totalTokens => throw _privateConstructorUsedError;
  @JsonKey(name: 'total_cost')
  double? get totalCost => throw _privateConstructorUsedError;

  /// Serializes this CostStats to a JSON map.
  Map<String, dynamic> toJson() => throw _privateConstructorUsedError;

  /// Create a copy of CostStats
  /// with the given fields replaced by the non-null parameter values.
  @JsonKey(includeFromJson: false, includeToJson: false)
  $CostStatsCopyWith<CostStats> get copyWith =>
      throw _privateConstructorUsedError;
}

/// @nodoc
abstract class $CostStatsCopyWith<$Res> {
  factory $CostStatsCopyWith(CostStats value, $Res Function(CostStats) then) =
      _$CostStatsCopyWithImpl<$Res, CostStats>;
  @useResult
  $Res call(
      {Map<String, dynamic>? daily,
      Map<String, dynamic>? monthly,
      @JsonKey(name: 'by_provider') Map<String, dynamic>? byProvider,
      @JsonKey(name: 'by_model') Map<String, dynamic>? byModel,
      @JsonKey(name: 'total_tokens') int? totalTokens,
      @JsonKey(name: 'total_cost') double? totalCost});
}

/// @nodoc
class _$CostStatsCopyWithImpl<$Res, $Val extends CostStats>
    implements $CostStatsCopyWith<$Res> {
  _$CostStatsCopyWithImpl(this._value, this._then);

  // ignore: unused_field
  final $Val _value;
  // ignore: unused_field
  final $Res Function($Val) _then;

  /// Create a copy of CostStats
  /// with the given fields replaced by the non-null parameter values.
  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? daily = freezed,
    Object? monthly = freezed,
    Object? byProvider = freezed,
    Object? byModel = freezed,
    Object? totalTokens = freezed,
    Object? totalCost = freezed,
  }) {
    return _then(_value.copyWith(
      daily: freezed == daily
          ? _value.daily
          : daily // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      monthly: freezed == monthly
          ? _value.monthly
          : monthly // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      byProvider: freezed == byProvider
          ? _value.byProvider
          : byProvider // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      byModel: freezed == byModel
          ? _value.byModel
          : byModel // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      totalTokens: freezed == totalTokens
          ? _value.totalTokens
          : totalTokens // ignore: cast_nullable_to_non_nullable
              as int?,
      totalCost: freezed == totalCost
          ? _value.totalCost
          : totalCost // ignore: cast_nullable_to_non_nullable
              as double?,
    ) as $Val);
  }
}

/// @nodoc
abstract class _$$CostStatsImplCopyWith<$Res>
    implements $CostStatsCopyWith<$Res> {
  factory _$$CostStatsImplCopyWith(
          _$CostStatsImpl value, $Res Function(_$CostStatsImpl) then) =
      __$$CostStatsImplCopyWithImpl<$Res>;
  @override
  @useResult
  $Res call(
      {Map<String, dynamic>? daily,
      Map<String, dynamic>? monthly,
      @JsonKey(name: 'by_provider') Map<String, dynamic>? byProvider,
      @JsonKey(name: 'by_model') Map<String, dynamic>? byModel,
      @JsonKey(name: 'total_tokens') int? totalTokens,
      @JsonKey(name: 'total_cost') double? totalCost});
}

/// @nodoc
class __$$CostStatsImplCopyWithImpl<$Res>
    extends _$CostStatsCopyWithImpl<$Res, _$CostStatsImpl>
    implements _$$CostStatsImplCopyWith<$Res> {
  __$$CostStatsImplCopyWithImpl(
      _$CostStatsImpl _value, $Res Function(_$CostStatsImpl) _then)
      : super(_value, _then);

  /// Create a copy of CostStats
  /// with the given fields replaced by the non-null parameter values.
  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? daily = freezed,
    Object? monthly = freezed,
    Object? byProvider = freezed,
    Object? byModel = freezed,
    Object? totalTokens = freezed,
    Object? totalCost = freezed,
  }) {
    return _then(_$CostStatsImpl(
      daily: freezed == daily
          ? _value._daily
          : daily // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      monthly: freezed == monthly
          ? _value._monthly
          : monthly // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      byProvider: freezed == byProvider
          ? _value._byProvider
          : byProvider // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      byModel: freezed == byModel
          ? _value._byModel
          : byModel // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      totalTokens: freezed == totalTokens
          ? _value.totalTokens
          : totalTokens // ignore: cast_nullable_to_non_nullable
              as int?,
      totalCost: freezed == totalCost
          ? _value.totalCost
          : totalCost // ignore: cast_nullable_to_non_nullable
              as double?,
    ));
  }
}

/// @nodoc
@JsonSerializable()
class _$CostStatsImpl implements _CostStats {
  const _$CostStatsImpl(
      {final Map<String, dynamic>? daily,
      final Map<String, dynamic>? monthly,
      @JsonKey(name: 'by_provider') final Map<String, dynamic>? byProvider,
      @JsonKey(name: 'by_model') final Map<String, dynamic>? byModel,
      @JsonKey(name: 'total_tokens') this.totalTokens,
      @JsonKey(name: 'total_cost') this.totalCost})
      : _daily = daily,
        _monthly = monthly,
        _byProvider = byProvider,
        _byModel = byModel;

  factory _$CostStatsImpl.fromJson(Map<String, dynamic> json) =>
      _$$CostStatsImplFromJson(json);

  final Map<String, dynamic>? _daily;
  @override
  Map<String, dynamic>? get daily {
    final value = _daily;
    if (value == null) return null;
    if (_daily is EqualUnmodifiableMapView) return _daily;
    // ignore: implicit_dynamic_type
    return EqualUnmodifiableMapView(value);
  }

  final Map<String, dynamic>? _monthly;
  @override
  Map<String, dynamic>? get monthly {
    final value = _monthly;
    if (value == null) return null;
    if (_monthly is EqualUnmodifiableMapView) return _monthly;
    // ignore: implicit_dynamic_type
    return EqualUnmodifiableMapView(value);
  }

  final Map<String, dynamic>? _byProvider;
  @override
  @JsonKey(name: 'by_provider')
  Map<String, dynamic>? get byProvider {
    final value = _byProvider;
    if (value == null) return null;
    if (_byProvider is EqualUnmodifiableMapView) return _byProvider;
    // ignore: implicit_dynamic_type
    return EqualUnmodifiableMapView(value);
  }

  final Map<String, dynamic>? _byModel;
  @override
  @JsonKey(name: 'by_model')
  Map<String, dynamic>? get byModel {
    final value = _byModel;
    if (value == null) return null;
    if (_byModel is EqualUnmodifiableMapView) return _byModel;
    // ignore: implicit_dynamic_type
    return EqualUnmodifiableMapView(value);
  }

  @override
  @JsonKey(name: 'total_tokens')
  final int? totalTokens;
  @override
  @JsonKey(name: 'total_cost')
  final double? totalCost;

  @override
  String toString() {
    return 'CostStats(daily: $daily, monthly: $monthly, byProvider: $byProvider, byModel: $byModel, totalTokens: $totalTokens, totalCost: $totalCost)';
  }

  @override
  bool operator ==(Object other) {
    return identical(this, other) ||
        (other.runtimeType == runtimeType &&
            other is _$CostStatsImpl &&
            const DeepCollectionEquality().equals(other._daily, _daily) &&
            const DeepCollectionEquality().equals(other._monthly, _monthly) &&
            const DeepCollectionEquality()
                .equals(other._byProvider, _byProvider) &&
            const DeepCollectionEquality().equals(other._byModel, _byModel) &&
            (identical(other.totalTokens, totalTokens) ||
                other.totalTokens == totalTokens) &&
            (identical(other.totalCost, totalCost) ||
                other.totalCost == totalCost));
  }

  @JsonKey(includeFromJson: false, includeToJson: false)
  @override
  int get hashCode => Object.hash(
      runtimeType,
      const DeepCollectionEquality().hash(_daily),
      const DeepCollectionEquality().hash(_monthly),
      const DeepCollectionEquality().hash(_byProvider),
      const DeepCollectionEquality().hash(_byModel),
      totalTokens,
      totalCost);

  /// Create a copy of CostStats
  /// with the given fields replaced by the non-null parameter values.
  @JsonKey(includeFromJson: false, includeToJson: false)
  @override
  @pragma('vm:prefer-inline')
  _$$CostStatsImplCopyWith<_$CostStatsImpl> get copyWith =>
      __$$CostStatsImplCopyWithImpl<_$CostStatsImpl>(this, _$identity);

  @override
  Map<String, dynamic> toJson() {
    return _$$CostStatsImplToJson(
      this,
    );
  }
}

abstract class _CostStats implements CostStats {
  const factory _CostStats(
      {final Map<String, dynamic>? daily,
      final Map<String, dynamic>? monthly,
      @JsonKey(name: 'by_provider') final Map<String, dynamic>? byProvider,
      @JsonKey(name: 'by_model') final Map<String, dynamic>? byModel,
      @JsonKey(name: 'total_tokens') final int? totalTokens,
      @JsonKey(name: 'total_cost') final double? totalCost}) = _$CostStatsImpl;

  factory _CostStats.fromJson(Map<String, dynamic> json) =
      _$CostStatsImpl.fromJson;

  @override
  Map<String, dynamic>? get daily;
  @override
  Map<String, dynamic>? get monthly;
  @override
  @JsonKey(name: 'by_provider')
  Map<String, dynamic>? get byProvider;
  @override
  @JsonKey(name: 'by_model')
  Map<String, dynamic>? get byModel;
  @override
  @JsonKey(name: 'total_tokens')
  int? get totalTokens;
  @override
  @JsonKey(name: 'total_cost')
  double? get totalCost;

  /// Create a copy of CostStats
  /// with the given fields replaced by the non-null parameter values.
  @override
  @JsonKey(includeFromJson: false, includeToJson: false)
  _$$CostStatsImplCopyWith<_$CostStatsImpl> get copyWith =>
      throw _privateConstructorUsedError;
}

AppConfig _$AppConfigFromJson(Map<String, dynamic> json) {
  return _AppConfig.fromJson(json);
}

/// @nodoc
mixin _$AppConfig {
  Map<String, dynamic>? get config => throw _privateConstructorUsedError;

  /// 服务端返回 float epoch（time.time()），不是 ISO 字符串。
  /// 用 double? 原样保存，避免 DateTime.parse 抛异常。
  double? get timestamp => throw _privateConstructorUsedError;

  /// Serializes this AppConfig to a JSON map.
  Map<String, dynamic> toJson() => throw _privateConstructorUsedError;

  /// Create a copy of AppConfig
  /// with the given fields replaced by the non-null parameter values.
  @JsonKey(includeFromJson: false, includeToJson: false)
  $AppConfigCopyWith<AppConfig> get copyWith =>
      throw _privateConstructorUsedError;
}

/// @nodoc
abstract class $AppConfigCopyWith<$Res> {
  factory $AppConfigCopyWith(AppConfig value, $Res Function(AppConfig) then) =
      _$AppConfigCopyWithImpl<$Res, AppConfig>;
  @useResult
  $Res call({Map<String, dynamic>? config, double? timestamp});
}

/// @nodoc
class _$AppConfigCopyWithImpl<$Res, $Val extends AppConfig>
    implements $AppConfigCopyWith<$Res> {
  _$AppConfigCopyWithImpl(this._value, this._then);

  // ignore: unused_field
  final $Val _value;
  // ignore: unused_field
  final $Res Function($Val) _then;

  /// Create a copy of AppConfig
  /// with the given fields replaced by the non-null parameter values.
  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? config = freezed,
    Object? timestamp = freezed,
  }) {
    return _then(_value.copyWith(
      config: freezed == config
          ? _value.config
          : config // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      timestamp: freezed == timestamp
          ? _value.timestamp
          : timestamp // ignore: cast_nullable_to_non_nullable
              as double?,
    ) as $Val);
  }
}

/// @nodoc
abstract class _$$AppConfigImplCopyWith<$Res>
    implements $AppConfigCopyWith<$Res> {
  factory _$$AppConfigImplCopyWith(
          _$AppConfigImpl value, $Res Function(_$AppConfigImpl) then) =
      __$$AppConfigImplCopyWithImpl<$Res>;
  @override
  @useResult
  $Res call({Map<String, dynamic>? config, double? timestamp});
}

/// @nodoc
class __$$AppConfigImplCopyWithImpl<$Res>
    extends _$AppConfigCopyWithImpl<$Res, _$AppConfigImpl>
    implements _$$AppConfigImplCopyWith<$Res> {
  __$$AppConfigImplCopyWithImpl(
      _$AppConfigImpl _value, $Res Function(_$AppConfigImpl) _then)
      : super(_value, _then);

  /// Create a copy of AppConfig
  /// with the given fields replaced by the non-null parameter values.
  @pragma('vm:prefer-inline')
  @override
  $Res call({
    Object? config = freezed,
    Object? timestamp = freezed,
  }) {
    return _then(_$AppConfigImpl(
      config: freezed == config
          ? _value._config
          : config // ignore: cast_nullable_to_non_nullable
              as Map<String, dynamic>?,
      timestamp: freezed == timestamp
          ? _value.timestamp
          : timestamp // ignore: cast_nullable_to_non_nullable
              as double?,
    ));
  }
}

/// @nodoc
@JsonSerializable()
class _$AppConfigImpl implements _AppConfig {
  const _$AppConfigImpl({final Map<String, dynamic>? config, this.timestamp})
      : _config = config;

  factory _$AppConfigImpl.fromJson(Map<String, dynamic> json) =>
      _$$AppConfigImplFromJson(json);

  final Map<String, dynamic>? _config;
  @override
  Map<String, dynamic>? get config {
    final value = _config;
    if (value == null) return null;
    if (_config is EqualUnmodifiableMapView) return _config;
    // ignore: implicit_dynamic_type
    return EqualUnmodifiableMapView(value);
  }

  /// 服务端返回 float epoch（time.time()），不是 ISO 字符串。
  /// 用 double? 原样保存，避免 DateTime.parse 抛异常。
  @override
  final double? timestamp;

  @override
  String toString() {
    return 'AppConfig(config: $config, timestamp: $timestamp)';
  }

  @override
  bool operator ==(Object other) {
    return identical(this, other) ||
        (other.runtimeType == runtimeType &&
            other is _$AppConfigImpl &&
            const DeepCollectionEquality().equals(other._config, _config) &&
            (identical(other.timestamp, timestamp) ||
                other.timestamp == timestamp));
  }

  @JsonKey(includeFromJson: false, includeToJson: false)
  @override
  int get hashCode => Object.hash(
      runtimeType, const DeepCollectionEquality().hash(_config), timestamp);

  /// Create a copy of AppConfig
  /// with the given fields replaced by the non-null parameter values.
  @JsonKey(includeFromJson: false, includeToJson: false)
  @override
  @pragma('vm:prefer-inline')
  _$$AppConfigImplCopyWith<_$AppConfigImpl> get copyWith =>
      __$$AppConfigImplCopyWithImpl<_$AppConfigImpl>(this, _$identity);

  @override
  Map<String, dynamic> toJson() {
    return _$$AppConfigImplToJson(
      this,
    );
  }
}

abstract class _AppConfig implements AppConfig {
  const factory _AppConfig(
      {final Map<String, dynamic>? config,
      final double? timestamp}) = _$AppConfigImpl;

  factory _AppConfig.fromJson(Map<String, dynamic> json) =
      _$AppConfigImpl.fromJson;

  @override
  Map<String, dynamic>? get config;

  /// 服务端返回 float epoch（time.time()），不是 ISO 字符串。
  /// 用 double? 原样保存，避免 DateTime.parse 抛异常。
  @override
  double? get timestamp;

  /// Create a copy of AppConfig
  /// with the given fields replaced by the non-null parameter values.
  @override
  @JsonKey(includeFromJson: false, includeToJson: false)
  _$$AppConfigImplCopyWith<_$AppConfigImpl> get copyWith =>
      throw _privateConstructorUsedError;
}
