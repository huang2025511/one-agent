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

    // 延迟发送配置（等悬浮窗 UI 加载完成）
    await Future.delayed(const Duration(seconds: 1));
    await _sendConfig(baseUrl, apiKey, sessionId);

    return true;
  }

  /// 关闭悬浮窗
  Future<void> hideOverlay() async {
    await FlutterOverlayWindow.closeOverlay();
    _isActive = false;
  }

  /// 发送配置到悬浮窗
  Future<void> _sendConfig(String baseUrl, String apiKey, String? sessionId) async {
    await FlutterOverlayWindow.shareData(
      jsonEncode({
        'type': 'config',
        'data': {
          'baseUrl': baseUrl,
          'apiKey': apiKey,
          'sessionId': sessionId,
        },
      }),
    );
  }

  /// 通知悬浮窗关闭
  Future<void> notifyClose() async {
    await FlutterOverlayWindow.shareData(
      jsonEncode({'type': 'close'}),
    );
    _isActive = false;
  }
}
