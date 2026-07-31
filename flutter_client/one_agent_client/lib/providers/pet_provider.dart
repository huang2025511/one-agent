import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/api_client.dart';
import '../services/overlay_pet_service.dart';
import 'live2d_model_provider.dart';
import 'settings_provider.dart';

/// 桌宠状态
class PetState {
  final bool isOverlayActive; // 悬浮窗是否激活
  final bool isLoading; // 正在启动/关闭
  final String? error;
  final String? lastBubble; // 最后一条气泡文字（主 APP 端记录）

  const PetState({
    this.isOverlayActive = false,
    this.isLoading = false,
    this.error,
    this.lastBubble,
  });

  PetState copyWith({
    bool? isOverlayActive,
    bool? isLoading,
    String? error,
    String? lastBubble,
  }) =>
      PetState(
        isOverlayActive: isOverlayActive ?? this.isOverlayActive,
        isLoading: isLoading ?? this.isLoading,
        error: error,
        lastBubble: lastBubble ?? this.lastBubble,
      );
}

/// 桌宠 Provider — 管理悬浮窗生命周期
class PetNotifier extends StateNotifier<PetState> {
  final Ref _ref;

  PetNotifier(this._ref) : super(const PetState()) {
    _init();
  }

  void _init() {
    // 监听来自悬浮窗的消息
    OverlayPetService.instance.messagesFromOverlay.listen((msg) {
      debugPrint('🐾 主APP收到桌宠消息: ${msg.type}');
      switch (msg.type) {
        case 'state':
          final bubble = msg.data['bubble'] as String?;
          if (bubble != null) {
            state = state.copyWith(lastBubble: bubble);
          }
          break;
      }
    });
  }

  /// 启动桌宠悬浮窗
  Future<void> startPet() async {
    if (state.isOverlayActive) return;

    state = state.copyWith(isLoading: true, error: null);

    try {
      final settings = _ref.read(settingsProvider);
      final baseUrl = settings.baseUrl;
      final apiKey = settings.apiKey;
      // 读取当前选中的 Live2D 模型路径（文件系统）
      final modelState = _ref.read(live2dModelProvider);
      final currentModel = modelState.currentModel;

      final success = await OverlayPetService.instance.show(
        width: 280,
        height: 360,
        config: {
          'baseUrl': baseUrl,
          'apiKey': apiKey,
          if (currentModel != null) ...{
            'modelPath': currentModel.dirPath,
            'modelFileName': currentModel.modelFileName,
          },
        },
      );

      state = state.copyWith(
        isOverlayActive: success,
        isLoading: false,
        error: success ? null : '无法启动悬浮窗，请检查权限',
      );
    } catch (e) {
      state = state.copyWith(
        isLoading: false,
        error: '启动失败: $e',
      );
    }
  }

  /// 关闭桌宠悬浮窗
  Future<void> stopPet() async {
    if (!state.isOverlayActive) return;

    state = state.copyWith(isLoading: true);
    try {
      await OverlayPetService.instance.hide();
      state = const PetState();
    } catch (e) {
      state = state.copyWith(
        isLoading: false,
        error: '关闭失败: $e',
      );
    }
  }

  /// 切换桌宠开关
  Future<void> toggle() async {
    if (state.isOverlayActive) {
      await stopPet();
    } else {
      await startPet();
    }
  }

  /// 刷新悬浮窗配置（模型切换后调用，悬浮窗会重新加载模型）
  Future<void> refreshConfig() async {
    if (!state.isOverlayActive) return;
    final settings = _ref.read(settingsProvider);
    final modelState = _ref.read(live2dModelProvider);
    final currentModel = modelState.currentModel;
    await OverlayPetService.instance.sendToOverlay(OverlayMessage(
      type: 'config',
      data: {
        'baseUrl': settings.baseUrl,
        'apiKey': settings.apiKey,
        if (currentModel != null) ...{
          'modelPath': currentModel.dirPath,
          'modelFileName': currentModel.modelFileName,
        },
      },
    ));
  }

  @override
  void dispose() {
    OverlayPetService.instance.dispose();
    super.dispose();
  }
}

final petProvider = StateNotifierProvider<PetNotifier, PetState>(
  (ref) => PetNotifier(ref),
);
