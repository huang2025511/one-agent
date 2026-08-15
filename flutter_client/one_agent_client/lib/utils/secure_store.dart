import 'package:flutter/services.dart';

/// 敏感值安全存储（Android Keystore 加密，见原生 SecureStorage）。
///
/// API Key 等敏感数据经 Android Keystore AES-256-GCM 加密后落盘，
/// 替代之前明文写 SharedPreferences 的做法。
///
/// 非原生环境（flutter test / 桌面）通道缺失时静默降级为无持久化。
class SecureStore {
  SecureStore._();

  static const _channel = MethodChannel('com.oneagent/secure_storage');

  /// 读取解密后的值；不存在或解密失败返回 null。
  static Future<String?> read(String key) async {
    try {
      return await _channel.invokeMethod<String>('read', {'key': key});
    } on Exception {
      return null;
    }
  }

  /// 写入（value 为空串等价于删除）。
  static Future<void> write(String key, String value) async {
    try {
      await _channel.invokeMethod('write', {'key': key, 'value': value});
    } on Exception {
      // 通道缺失（测试环境）：仅内存态，不持久化
    }
  }

  /// 删除。
  static Future<void> delete(String key) async {
    try {
      await _channel.invokeMethod('delete', {'key': key});
    } on Exception {
      // 同上
    }
  }
}
