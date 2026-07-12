// ignore_for_file: constant_identifier_names

/// One-Agent API 配置常量
class ApiConstants {
  ApiConstants._();

  /// 默认服务器地址（用户可在设置中修改）
  /// 注意：默认使用 localhost，仅适用于模拟器或本机调试
  /// 真机使用时请在设置中改为服务器的实际 IP 或域名
  static const String defaultBaseUrl = 'http://127.0.0.1:18792';

  /// 默认 Web UI 地址
  static const String defaultWebUrl = 'http://127.0.0.1:18791';

  /// API 超时（秒）
  static const int connectTimeout = 10;
  static const int receiveTimeout = 60;
  static const int sendTimeout = 30;

  /// SSE 流式超时（秒）
  static const int streamTimeout = 180;

  // ── API 端点 ──────────────────────────────────────────────

  static const String chat = '/api/chat';
  static const String chatStream = '/api/chat/stream';
  static const String sessions = '/api/sessions';
  static const String memorySearch = '/api/memory/search';
  static const String memoryAdd = '/api/memory/add';
  static const String memoryPage = '/api/memory/page';
  static const String skills = '/api/skills';
  static const String marketplace = '/api/marketplace';
  static const String marketplaceInstall = '/api/marketplace/install';
  static const String stats = '/api/stats';
  static const String metrics = '/api/metrics';
  static const String health = '/api/health';
  static const String config = '/api/config';
  static const String cacheClear = '/api/cache/clear';

  /// 审批
  static const String approvalsPending = '/api/approvals/pending';
  static const String approvalsApprove = '/api/approvals';
  static const String approvalsDeny = '/api/approvals';

  /// MCP
  static const String mcpTools = '/api/mcp/tools';
  static const String mcpStatus = '/api/mcp/status';

  /// 成本
  static const String costsDaily = '/api/costs/daily';
  static const String costsMonthly = '/api/costs/monthly';
  static const String costsBudget = '/api/costs/budget';

  /// 审计
  static const String audit = '/api/audit';
}

/// SharedPreferences 键名
class PrefKeys {
  PrefKeys._();

  static const String baseUrl = 'base_url';
  static const String apiKey = 'api_key';
  static const String themeMode = 'theme_mode';
  static const String language = 'language';
  static const String lastSessionId = 'last_session_id';
}

/// 主题模式
enum AppThemeMode { system, light, dark }
