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

  const SettingsState({
    this.baseUrl = ApiConstants.defaultBaseUrl,
    this.apiKey = '',
    this.isConnected = false,
    this.isLoading = false,
  });

  SettingsState copyWith({
    String? baseUrl,
    String? apiKey,
    bool? isConnected,
    bool? isLoading,
  }) => SettingsState(
    baseUrl: baseUrl ?? this.baseUrl,
    apiKey: apiKey ?? this.apiKey,
    isConnected: isConnected ?? this.isConnected,
    isLoading: isLoading ?? this.isLoading,
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
    state = state.copyWith(baseUrl: url, apiKey: key);
    ApiClient.configure(baseUrl: url, apiKey: key);
    await checkConnection();
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
