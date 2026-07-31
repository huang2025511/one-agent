import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:flutter_overlay_window/flutter_overlay_window.dart';

/// 主 APP ↔ 悬浮窗通信消息
@immutable
class OverlayMessage {
  final String type; // 'config' | 'close' | 'chat_request' | 'chat_reply' | 'state'
  final Map<String, dynamic> data;

  const OverlayMessage({required this.type, this.data = const {}});

  String toJson() => jsonEncode({'type': type, 'data': data});

  static OverlayMessage? fromJson(String jsonStr) {
    try {
      final json = jsonDecode(jsonStr) as Map<String, dynamic>;
      return OverlayMessage(
        type: json['type'] as String,
        data: (json['data'] as Map<String, dynamic>?) ?? const {},
      );
    } catch (_) {
      return null;
    }
  }
}

/// 桌宠悬浮窗服务
///
/// 职责：
/// - 检查/请求 SYSTEM_ALERT_WINDOW 权限
/// - 启动/停止悬浮窗
/// - 主 APP ↔ 悬浮窗双向通信
class OverlayPetService {
  OverlayPetService._();
  static final OverlayPetService instance = OverlayPetService._();

  StreamSubscription? _listenerSub;
  final _messageFromOverlayController =
      StreamController<OverlayMessage>.broadcast();

  /// 来自悬浮窗的消息流（主 APP 监听）
  Stream<OverlayMessage> get messagesFromOverlay =>
      _messageFromOverlayController.stream;

  /// 悬浮窗是否正在运行
  bool get isActive => _listenerSub != null;

  /// 检查悬浮窗权限
  Future<bool> checkPermission() async {
    return await FlutterOverlayWindow.isPermissionGranted() ?? false;
  }

  /// 请求悬浮窗权限
  Future<bool> requestPermission() async {
    final granted = await FlutterOverlayWindow.requestPermission();
    return granted ?? false;
  }

  /// 启动桌宠悬浮窗
  ///
  /// [width] [height] 悬浮窗尺寸
  /// [config] 传递给悬浮窗的配置（baseUrl, apiKey, sessionId 等）
  Future<bool> show({
    int width = 200,
    int height = 200,
    Map<String, dynamic> config = const {},
  }) async {
    // 1. 确保有权限
    final hasPermission = await checkPermission();
    if (!hasPermission) {
      final granted = await requestPermission();
      if (!granted) {
        debugPrint('❌ 悬浮窗权限被拒绝');
        return false;
      }
    }

    // 2. 监听来自悬浮窗的消息
    _listenerSub?.cancel();
    _listenerSub = FlutterOverlayWindow.overlayListener.listen((event) {
      debugPrint('📩 主APP收到悬浮窗消息: $event');
      final msg = OverlayMessage.fromJson(event.toString());
      if (msg != null) {
        _messageFromOverlayController.add(msg);
      }
    });

    // 3. 显示悬浮窗
    final result = await FlutterOverlayWindow.showOverlay(
      enableDrag: true,
      overlayTitle: 'One-Agent 桌宠',
      overlayContent: '桌宠运行中',
      flag: OverlayFlag.defaultFlag,
      visibility: NotificationVisibility.visibilityPublic,
      positionGravity: PositionGravity.auto,
      height: height,
      width: width,
      startPosition: const OverlayPosition(0, 0),
    );

    debugPrint('✅ 悬浮窗启动结果: $result');

    // 4. 等悬浮窗渲染后发送配置
    await Future.delayed(const Duration(milliseconds: 500));
    await sendToOverlay(OverlayMessage(
      type: 'config',
      data: config,
    ));

    return result ?? false;
  }

  /// 关闭悬浮窗
  Future<bool> hide() async {
    await sendToOverlay(const OverlayMessage(type: 'close'));
    await Future.delayed(const Duration(milliseconds: 100));
    _listenerSub?.cancel();
    _listenerSub = null;
    final result = await FlutterOverlayWindow.closeOverlay();
    debugPrint('✅ 悬浮窗关闭结果: $result');
    return result ?? false;
  }

  /// 向悬浮窗发送消息
  Future<void> sendToOverlay(OverlayMessage msg) async {
    await FlutterOverlayWindow.shareData(msg.toJson());
  }

  void dispose() {
    _listenerSub?.cancel();
    _listenerSub = null;
    _messageFromOverlayController.close();
  }
}
