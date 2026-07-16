import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../api/api_client.dart';
import '../utils/constants.dart';

/// 设置状态
class SettingsState {
  final String baseUrl;
  final String apiKey;
  final bool isConnected;
  final bool isLoading;
  /// 文字缩放系数（1.0 为默认）。问题10：客户端文字设置功能。
  final double fontScale;

  const SettingsState({
    this.baseUrl = ApiConstants.defaultBaseUrl,
    this.apiKey = '',
    this.isConnected = false,
    this.isLoading = false,
    this.fontScale = 1.0,
  });

  SettingsState copyWith({
    String? baseUrl,
    String? apiKey,
    bool? isConnected,
    bool? isLoading,
    double? fontScale,
  }) => SettingsState(
    baseUrl: baseUrl ?? this.baseUrl,
    apiKey: apiKey ?? this.apiKey,
    isConnected: isConnected ?? this.isConnected,
    isLoading: isLoading ?? this.isLoading,
    fontScale: fontScale ?? this.fontScale,
  );
}

/// 设置 Provider
class SettingsNotifier extends StateNotifier<SettingsState> {
  SettingsNotifier() : super(const SettingsState()) {
    _load();
  }

  Future<void> _load() async {
    final prefs = await SharedPreferences.getInstance();
    final url = prefs.getString(PrefKeys.baseUrl) ?? ApiConstants.defaultBaseUrl;
    final key = prefs.getString(PrefKeys.apiKey) ?? '';
    final scale = prefs.getDouble(PrefKeys.fontScale) ?? 1.0;
    state = state.copyWith(baseUrl: url, apiKey: key, fontScale: scale);
    ApiClient.configure(baseUrl: url, apiKey: key);
    await checkConnection();
  }

  /// 问题10：设置文字缩放系数，持久化到本地
  Future<void> setFontScale(double scale) async {
    final clamped = scale.clamp(0.8, 1.6);
    final prefs = await SharedPreferences.getInstance();
    await prefs.setDouble(PrefKeys.fontScale, clamped);
    state = state.copyWith(fontScale: clamped);
  }

  Future<void> setBaseUrl(String url) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(PrefKeys.baseUrl, url);
    state = state.copyWith(baseUrl: url);
    ApiClient.configure(baseUrl: url, apiKey: state.apiKey);
    await checkConnection();
  }

  Future<void> setApiKey(String key) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(PrefKeys.apiKey, key);
    state = state.copyWith(apiKey: key);
    ApiClient.configure(baseUrl: state.baseUrl, apiKey: key);
    await checkConnection();
  }

  Future<bool> checkConnection() async {
    state = state.copyWith(isLoading: true);
    final ok = await ApiClient.checkConnection();
    state = state.copyWith(isConnected: ok, isLoading: false);
    return ok;
  }
}

final settingsProvider = StateNotifierProvider<SettingsNotifier, SettingsState>(
  (ref) => SettingsNotifier(),
);
