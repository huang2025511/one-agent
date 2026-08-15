// ignore_for_file: constant_identifier_names

/// One-Agent API 配置常量
class ApiConstants {
  ApiConstants._();

  /// 默认服务器地址（用户可在设置中修改）
  /// 注意：默认使用 localhost，仅适用于模拟器或本机调试
  /// 真机使用时请在设置中改为服务器的实际 IP 或域名
  static const String defaultBaseUrl = 'http://127.0.0.1:18792';

  /// API 超时（秒）
  static const int connectTimeout = 10;
  static const int receiveTimeout = 60;
  static const int sendTimeout = 30;

  // ── API 端点（仅保留实际被引用的；其余端点在 api/ 层直接硬编码路径）──

  static const String chatStream = '/api/chat/stream';
  static const String health = '/api/health';
}

/// SharedPreferences 键名
class PrefKeys {
  PrefKeys._();

  static const String baseUrl = 'base_url';
  static const String apiKey = 'api_key';
  static const String fontScale = 'font_scale';
}
