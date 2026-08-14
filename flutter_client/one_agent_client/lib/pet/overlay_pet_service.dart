import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_overlay_window/flutter_overlay_window.dart';

/// 悬浮窗宠物服务
///
/// 负责请求权限、显示/关闭悬浮窗、主APP与悬浮窗之间通信
class OverlayPetService {
  static final OverlayPetService _instance = OverlayPetService._();
  factory OverlayPetService() => _instance;
  OverlayPetService._();

  bool _isActive = false;
  bool get isActive => _isActive;

  // 主 APP 对悬浮窗消息的监听（响应悬浮窗 ready 信号后补发配置）
  StreamSubscription<dynamic>? _mainAppSub;

  // 最近一次下发的配置，供悬浮窗就绪后补发
  String? _lastBaseUrl;
  String? _lastApiKey;
  String? _lastSessionId;

  /// 检查并请求悬浮窗权限
  Future<bool> requestPermission() async {
    final granted = await FlutterOverlayWindow.isPermissionGranted();
    if (!granted) {
      final result = await FlutterOverlayWindow.requestPermission();
      return result ?? false;
    }
    return true;
  }

  /// 显示悬浮窗宠物
  Future<bool> showOverlay({
    required String baseUrl,
    required String apiKey,
    String? sessionId,
  }) async {
    // 检查权限
    final hasPermission = await requestPermission();
    if (!hasPermission) {
      debugPrint('悬浮窗权限未授予');
      return false;
    }

    _lastBaseUrl = baseUrl;
    _lastApiKey = apiKey;
    _lastSessionId = sessionId;

    // 先注册主 APP 监听，确保悬浮窗发送的 ready 信号不会被丢弃
    _ensureMainAppListener();

    // 显示悬浮窗
    await FlutterOverlayWindow.showOverlay(
      enableDrag: true,
      overlayTitle: 'One-Agent 桌宠',
      overlayContent: '桌宠运行中',
      // focusPointer：允许悬浮窗内的输入框获取键盘焦点。
      // 之前误用 OverlayFlag.focusable（不存在的枚举）导致构建失败，
      // 回退到 defaultFlag 后又导致输入框无法弹键盘。
      flag: OverlayFlag.focusPointer,
      visibility: NotificationVisibility.visibilityPublic,
      positionGravity: PositionGravity.none,
      // 初始位置放屏幕右侧中部，避免挡状态栏/底部导航
      alignment: OverlayAlignment.centerRight,
      height: 250,
      width: 300,
    );

    _isActive = true;

    // 延迟发送配置（等悬浮窗 UI 加载完成）。悬浮窗初始化完成后还会
    // 通过 ready 信号再次补发，避免首次配置因监听器未注册而丢失。
    await Future.delayed(const Duration(seconds: 1));
    await _sendConfig(baseUrl, apiKey, sessionId);

    return true;
  }

  /// 关闭悬浮窗
  Future<void> hideOverlay() async {
    await FlutterOverlayWindow.closeOverlay();
    _isActive = false;
  }

  /// 注册主 APP 对悬浮窗消息的监听。
  ///
  /// 悬浮窗加载完成后会发送 `ready` 信号，收到后补发配置，
  /// 防止悬浮窗引擎初始化慢于上面的 1 秒延迟导致配置丢失、聊天不可用。
  void _ensureMainAppListener() {
    if (_mainAppSub != null) return;
    _mainAppSub = FlutterOverlayWindow.overlayListener.listen((event) {
      try {
        final json = decodeOverlayMessage(event);
        if (json != null && json['type'] == 'ready') {
          _sendConfig(_lastBaseUrl ?? '', _lastApiKey ?? '', _lastSessionId);
        }
      } catch (e) {
        debugPrint('主APP监听悬浮窗消息失败: $e');
      }
    });
  }

  /// 发送配置到悬浮窗
  ///
  /// 直接传 Map 给 BasicMessageChannel(JSONMessageCodec)，避免
  /// 先 jsonEncode 再编码导致的 JSON 双重编码。
  Future<void> _sendConfig(String baseUrl, String apiKey, String? sessionId) async {
    await FlutterOverlayWindow.shareData({
      'type': 'config',
      'data': {
        'baseUrl': baseUrl,
        'apiKey': apiKey,
        'sessionId': sessionId,
      },
    });
  }

  /// 通知悬浮窗关闭
  Future<void> notifyClose() async {
    await FlutterOverlayWindow.shareData({'type': 'close'});
    _isActive = false;
  }
}

/// 将 BasicMessageChannel 收到的消息解析为 Map。
///
/// shareData 现在直接传 Map（推荐），JSONMessageCodec 解码后即为 Map；
/// 兼容旧代码传入的 JSON 字符串。
Map<String, dynamic>? decodeOverlayMessage(dynamic event) {
  try {
    if (event is Map) {
      return Map<String, dynamic>.from(event);
    }
    if (event is String) {
      final decoded = jsonDecode(event);
      if (decoded is Map) return Map<String, dynamic>.from(decoded);
    }
  } catch (_) {
    // 忽略解析失败，由调用方处理
  }
  return null;
}
