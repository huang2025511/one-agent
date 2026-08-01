import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/api_client.dart';
import '../services/overlay_pet_service.dart';
import 'live2d_model_provider.dart';
import 'settings_provider.dart';

/// 内置免费 Live2D 模型（mao 猫）的 assets 路径
/// 当用户未导入自定义模型时，悬浮窗使用此内置模型
const _kBuiltinModelDir = 'assets/models/mao/';
const _kBuiltinModelFile = 'mao_pro.model3.json';

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

      // 模型路径：优先用户导入的模型，否则用内置 mao 猫模型（assets 路径）
      // ⚠️ 必须显式传入，否则悬浮窗独立引擎的 rootBundle 可能无法
      // 正确扫描 AssetManifest，导致 PetRenderer 找不到模型而显示 Canvas fallback
      final modelPath = currentModel?.dirPath ?? _kBuiltinModelDir;
      final modelFileName = currentModel?.modelFileName ?? _kBuiltinModelFile;

      // 整体加 15 秒超时保护，防止任何环节卡死导致按钮永远灰色
      final success = await OverlayPetService.instance.show(
        width: 280,
        height: 360,
        config: {
          'baseUrl': baseUrl,
          'apiKey': apiKey,
          'modelPath': modelPath,
          'modelFileName': modelFileName,
        },
      ).timeout(const Duration(seconds: 15), onTimeout: () {
        debugPrint('⏰ startPet show() 整体超时（15s）');
        return false;
      });

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
    final modelPath = currentModel?.dirPath ?? _kBuiltinModelDir;
    final modelFileName = currentModel?.modelFileName ?? _kBuiltinModelFile;
    await OverlayPetService.instance.sendToOverlay(OverlayMessage(
      type: 'config',
      data: {
        'baseUrl': settings.baseUrl,
        'apiKey': settings.apiKey,
        'modelPath': modelPath,
        'modelFileName': modelFileName,
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
