import 'package:flutter/services.dart';

/// 桌宠悬浮窗服务（原生 WebView + Live2D 渲染）
///
/// 通过 MethodChannel 调用 Android 原生 [PetOverlayService]：
/// 系统悬浮窗 + WebView 渲染 Live2D 模型（pixi-live2d-display），
/// 支持 SSE 流式聊天、气泡消息、点击互动、拖动、关闭。
///
/// 之前的 flutter_overlay_window（Flutter 引擎渲染）方案在系统悬浮窗中
/// 只能显示简单 Canvas 图形（"圆形宠物"），无法渲染 Live2D 模型，
/// 已替换为开源社区验证过的原生 WebView 方案。
class OverlayPetService {
  static final OverlayPetService _instance = OverlayPetService._();
  factory OverlayPetService() => _instance;
  OverlayPetService._();

  static const _channel = MethodChannel('com.oneagent/pet_overlay');

  bool _isActive = false;
  bool get isActive => _isActive;

  /// 检查悬浮窗权限是否已授予
  Future<bool> isPermissionGranted() async {
    try {
      return await _channel.invokeMethod<bool>('isPermissionGranted') ?? false;
    } on PlatformException {
      return false;
    }
  }

  /// 显示桌宠悬浮窗
  ///
  /// 未授权悬浮窗权限时，原生侧会跳转系统设置页并在授权后自动启动，
  /// 此时返回 false（本次调用未直接启动）。
  ///
  /// [modelPath]/[modelFileName] 为用户导入的 Live2D 模型路径，
  /// 为空时使用内置 Hiyori 模型。
  Future<bool> showOverlay({
    required String baseUrl,
    required String apiKey,
    String? modelPath,
    String? modelFileName,
  }) async {
    try {
      final ok = await _channel.invokeMethod<bool>('show', {
        'baseUrl': baseUrl,
        'apiKey': apiKey,
        'modelPath': modelPath,
        'modelFileName': modelFileName,
      });
      _isActive = ok == true;
      return _isActive;
    } on PlatformException {
      return false;
    }
  }

  /// 关闭悬浮窗
  Future<void> hideOverlay() async {
    try {
      await _channel.invokeMethod('hide');
    } on PlatformException catch (_) {}
    _isActive = false;
  }

  /// 悬浮窗运行中切换模型（实时生效）
  ///
  /// 参数为空时切回内置 Hiyori 模型。
  Future<void> updateModel({String? modelPath, String? modelFileName}) async {
    try {
      await _channel.invokeMethod('updateModel', {
        'modelPath': modelPath,
        'modelFileName': modelFileName,
      });
    } on PlatformException catch (_) {}
  }

  /// 同步真实运行状态
  ///
  /// 悬浮窗可能被用户通过 ✕ 按钮或系统回收关闭，
  /// 操作前先同步一次，避免本地状态与原生不一致。
  Future<void> syncState() async {
    try {
      _isActive = await _channel.invokeMethod<bool>('isActive') ?? false;
    } on PlatformException {
      _isActive = false;
    }
  }
}
