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

  /// 不要在 dispose 关闭 broadcast controller，
  /// 因为 PetNotifier 会被重建，但 OverlayPetService 是单例，
  /// 关闭后重建的 PetNotifier 监听时会 "stream has already been listened to" / "Bad state: Stream has already been listened to"。
  /// 如果 controller 被关闭（isClosed），用 _reinitStreamController() 重新创建。
  StreamController<OverlayMessage> _messageFromOverlayController =
      StreamController<OverlayMessage>.broadcast();

  /// 幂等：确保 stream controller 可订阅
  void _reinitStreamControllerIfClosed() {
    if (_messageFromOverlayController.isClosed) {
      debugPrint('🔄 _messageFromOverlayController 已关闭，重新创建');
      _messageFromOverlayController =
          StreamController<OverlayMessage>.broadcast();
    }
  }

  /// 来自悬浮窗的消息流（主 APP 监听）
  Stream<OverlayMessage> get messagesFromOverlay {
    _reinitStreamControllerIfClosed();
    return _messageFromOverlayController.stream;
  }

  /// 悬浮窗是否正在运行
  bool get isActive => _listenerSub != null;

  /// 检查悬浮窗权限
  Future<bool> checkPermission() async {
    return await FlutterOverlayWindow.isPermissionGranted() ?? false;
  }

  /// 请求悬浮窗权限（授权弹窗返回后二次校验，修复部分机型 requestPermission 返回假 false）
  Future<bool> requestPermissionWithRetry() async {
    final rawGranted = await FlutterOverlayWindow.requestPermission();
    if (rawGranted == true) return true;

    // 部分 Android 机型：用户刚在设置页面授权，requestPermission 仍然返回 false，
    // 等 300ms 再查一次 isPermissionGranted
    await Future.delayed(const Duration(milliseconds: 300));
    final recheck = await FlutterOverlayWindow.isPermissionGranted();
    if (recheck == true) {
      debugPrint('✅ 权限二次校验通过（首次返回 false 但实际已授权）');
      return true;
    }

    // 再等 800ms 做最后一次检查（覆盖系统设置跳转返回慢的机型）
    await Future.delayed(const Duration(milliseconds: 800));
    return await FlutterOverlayWindow.isPermissionGranted() ?? false;
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
    _reinitStreamControllerIfClosed();

    // 1. 确保有权限（请求授权后二次检查）
    final hasPermission = await checkPermission();
    if (!hasPermission) {
      final granted = await requestPermissionWithRetry();
      if (!granted) {
        debugPrint('❌ 悬浮窗权限被拒绝');
        return false;
      }
      // 授权成功后再查一次，保证权限一定到位
      final finalCheck = await checkPermission();
      if (!finalCheck) {
        debugPrint('❌ 最终权限校验失败，请检查系统设置');
        return false;
      }
    }

    // 2. 监听来自悬浮窗的消息
    //    FlutterOverlayWindow.overlayListener 是单次订阅流（非 broadcast），
    //    必须确保上一次完全 cancel 完成后（等 Future）才能重新 listen，
    //    否则抛 "Bad state: Stream has already been listened to"。
    await _listenerSub?.cancel();
    _listenerSub = null;
    try {
      _listenerSub = FlutterOverlayWindow.overlayListener.listen((event) {
        debugPrint('📩 主APP收到悬浮窗消息: $event');
        final msg = OverlayMessage.fromJson(event.toString());
        if (msg != null) {
          _reinitStreamControllerIfClosed();
          _messageFromOverlayController.add(msg);
        }
      }, onError: (e) {
        debugPrint('⚠️  overlayListener 流错误: $e');
      });
    } catch (e) {
      // 兜底：如果 overlayListener 仍报重复监听，跳过监听（不影响启动，只是收不到消息）
      debugPrint('⚠️  跳过 overlayListener 监听（已订阅）: $e');
      _listenerSub = null;
    }

    // 3. 显示悬浮窗（加 10 秒超时，防止 showOverlay 挂起导致按钮永远灰色）
    try {
      await FlutterOverlayWindow.showOverlay(
        enableDrag: true,
        overlayTitle: 'One-Agent 桌宠',
        overlayContent: '桌宠运行中',
        flag: OverlayFlag.defaultFlag,
        visibility: NotificationVisibility.visibilityPublic,
        positionGravity: PositionGravity.auto,
        height: height,
        width: width,
        startPosition: const OverlayPosition(0, 0),
      ).timeout(const Duration(seconds: 10), onTimeout: () {
        debugPrint('⏰ showOverlay 超时（10s），继续执行');
      });
    } catch (e) {
      debugPrint('❌ showOverlay 异常: $e');
      // 不直接 return false，因为某些设备上 showOverlay 即使成功也可能抛异常
    }

    debugPrint('✅ 悬浮窗已请求显示');

    // 4. 等悬浮窗渲染后发送配置
    await Future.delayed(const Duration(milliseconds: 500));
    await sendToOverlay(OverlayMessage(
      type: 'config',
      data: config,
    ));

    return true;
  }

  /// 关闭悬浮窗
  Future<bool> hide() async {
    try {
      await sendToOverlay(const OverlayMessage(type: 'close'));
    } catch (_) {}
    await Future.delayed(const Duration(milliseconds: 100));
    // cancel 必须 await 完成后才能把 _listenerSub 置 null
    await _listenerSub?.cancel();
    _listenerSub = null;
    try {
      await FlutterOverlayWindow.closeOverlay();
    } catch (_) {}
    debugPrint('✅ 悬浮窗已关闭');
    return true;
  }

  /// 向悬浮窗发送消息
  Future<void> sendToOverlay(OverlayMessage msg) async {
    await FlutterOverlayWindow.shareData(msg.toJson());
  }

  /// ⚠️  不再关闭 broadcast controller，
  /// 因为 PetNotifier 会被 Riverpod 重创建，再次订阅会失败。
  /// 单例 OverlayPetService 生命周期与 App 进程绑定即可。
  void dispose() {
    _listenerSub?.cancel();
    _listenerSub = null;
    // 注释掉以下行以避免 "stream has already been listened to":
    // _messageFromOverlayController.close();
  }
}
