import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import '../utils/constants.dart';

/// Dio 全局实例 + 拦截器配置
class ApiClient {
  ApiClient._();

  static Dio? _dio;
  static String _baseUrl = ApiConstants.defaultBaseUrl;
  static String _apiKey = '';

  /// 获取配置好的 Dio 实例
  static Dio get dio {
    _dio ??= _createDio();
    return _dio!;
  }

  /// 重新配置 baseUrl 和 apiKey
  static void configure({required String baseUrl, String? apiKey}) {
    _baseUrl = baseUrl;
    if (apiKey != null) _apiKey = apiKey;
    _dio = _createDio();
  }

  static Dio _createDio() {
    final d = Dio(BaseOptions(
      baseUrl: _baseUrl,
      connectTimeout: const Duration(seconds: ApiConstants.connectTimeout),
      receiveTimeout: const Duration(seconds: ApiConstants.receiveTimeout),
      sendTimeout: const Duration(seconds: ApiConstants.sendTimeout),
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
      },
    ));

    // 请求拦截器：注入 API Key
    d.interceptors.add(InterceptorsWrapper(
      onRequest: (options, handler) {
        if (_apiKey.isNotEmpty) {
          options.headers['X-API-Key'] = _apiKey;
        }
        if (kDebugMode) {
          debugPrint('➡️ [${options.method}] ${options.uri}');
        }
        handler.next(options);
      },
      onResponse: (response, handler) {
        if (kDebugMode) {
          debugPrint('✅ [${response.statusCode}] ${response.requestOptions.uri}');
        }
        handler.next(response);
      },
      onError: (err, handler) {
        if (kDebugMode) {
          debugPrint('❌ [${err.response?.statusCode}] ${err.requestOptions.uri} => ${err.message}');
        }
        handler.next(err);
      },
    ));

    return d;
  }

  /// 检查连接是否可用
  static Future<bool> checkConnection() async {
    try {
      final resp = await dio.get(ApiConstants.health,
          options: Options(receiveTimeout: const Duration(seconds: 5)));
      return resp.statusCode == 200;
    } catch (_) {
      return false;
    }
  }
}
