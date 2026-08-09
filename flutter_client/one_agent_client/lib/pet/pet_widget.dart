import 'package:flutter/material.dart';
import 'package:rive/rive.dart';

import 'pet_brain.dart';

/// Rive 宠物渲染 Widget
///
/// 加载 .riv 文件，根据 [PetMood] 和 [PetAction] 切换动画。
/// 如果 Rive 加载失败，自动 fallback 到 Canvas 绘制的猫。
class PetWidget extends StatefulWidget {
  final PetMood mood;
  final PetAction action;
  final double size;

  const PetWidget({
    super.key,
    required this.mood,
    required this.action,
    this.size = 150,
  });

  @override
  State<PetWidget> createState() => _PetWidgetState();
}

class _PetWidgetState extends State<PetWidget> {
  // Rive 运行时控制器
  RiveArtboard? _artboard;
  SMIInput<bool>? _idleInput;
  SMIInput<bool>? _talkInput;
  SMIInput<bool>? _happyInput;
  SMIInput<bool>? _sleepInput;
  SMIInput<bool>? _thinkInput;
  SMIInput<double>? _lookInput; // 控制左右看的数值输入

  bool _loadFailed = false;
  String _activeAnimation = '';

  @override
  void initState() {
    super.initState();
    _loadRive();
  }

  @override
  void didUpdateWidget(PetWidget oldWidget) {
    super.didUpdateWidget(oldWidget);
    // 状态变化时切换动画
    if (oldWidget.mood != widget.mood || oldWidget.action != widget.action) {
      _updateAnimation();
    }
  }

  void _loadRive() async {
    try {
      final data = await rootBundle.load('assets/rive/bouncy_cat.riv');
      final file = RiveFile.import(data);
      final artboard = file.mainArtboard;

      // 尝试找到状态机
      if (artboard is RuntimeArtboard) {
        // 遍历控制器，找到 SMI 输入
        for (final controller in artboard.controllers) {
          if (controller is SMILinearAnimationInstance) {
            // 线性动画实例
          }
        }
      }

      // 尝试创建状态机控制器
      final smiController = StateMachineController.fromArtboard(
        artboard,
        'State Machine 1',
      );

      if (smiController != null) {
        artboard.addController(smiController);
        _idleInput = smiController.findInput<bool>('idle') as SMIInput<bool>?;
        _talkInput = smiController.findInput<bool>('talk') as SMIInput<bool>?;
        _happyInput = smiController.findInput<bool>('happy') as SMIInput<bool>?;
        _sleepInput = smiController.findInput<bool>('sleep') as SMIInput<bool>?;
        _thinkInput = smiController.findInput<bool>('think') as SMIInput<bool>?;
        _lookInput = smiController.findInput<double>('look') as SMIInput<double>?;
      }

      if (mounted) {
        setState(() {
          _artboard = artboard;
        });
        _updateAnimation();
      }
    } catch (e) {
      debugPrint('Rive 加载失败: $e');
      if (mounted) {
        setState(() => _loadFailed = true);
      }
    }
  }

  void _updateAnimation() {
    if (_artboard == null) return;

    // 重置所有布尔输入
    _idleInput?.value = false;
    _talkInput?.value = false;
    _happyInput?.value = false;
    _sleepInput?.value = false;
    _thinkInput?.value = false;

    switch (widget.action) {
      case PetAction.idle:
      case PetAction.blink:
        _idleInput?.value = true;
        _activeAnimation = 'idle';
        break;
      case PetAction.lookAround:
        _idleInput?.value = true;
        _lookInput?.value = widget.mood == PetMood.curious ? 1.0 : 0.0;
        _activeAnimation = 'lookAround';
        break;
      case PetAction.tilt:
        _idleInput?.value = true;
        _activeAnimation = 'tilt';
        break;
      case PetAction.bounce:
        _happyInput?.value = true;
        _activeAnimation = 'bounce';
        break;
      case PetAction.wave:
        _happyInput?.value = true;
        _activeAnimation = 'wave';
        break;
      case PetAction.sleep:
        _sleepInput?.value = true;
        _activeAnimation = 'sleep';
        break;
      case PetAction.talk:
        _talkInput?.value = true;
        _activeAnimation = 'talk';
        break;
      case PetAction.think:
        _thinkInput?.value = true;
        _activeAnimation = 'think';
        break;
      case PetAction.happy:
        _happyInput?.value = true;
        _activeAnimation = 'happy';
        break;
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loadFailed) {
      // Fallback: Canvas 绘制的简单猫
      return SizedBox(
        width: widget.size,
        height: widget.size,
        child: CustomPaint(
          painter: _FallbackCatPainter(mood: widget.mood),
        ),
      );
    }

    if (_artboard == null) {
      return SizedBox(
        width: widget.size,
        height: widget.size,
        child: const Center(
          child: CircularProgressIndicator(strokeWidth: 2),
        ),
      );
    }

    return SizedBox(
      width: widget.size,
      height: widget.size,
      child: Rive(
        artboard: _artboard!,
        fit: BoxFit.contain,
        alignment: Alignment.center,
      ),
    );
  }
}

/// Canvas Fallback 猫（Rive 加载失败时使用）
class _FallbackCatPainter extends CustomPainter {
  final PetMood mood;

  _FallbackCatPainter({required this.mood});

  @override
  void paint(Canvas canvas, Size size) {
    final cx = size.width / 2;
    final cy = size.height / 2;
    final r = size.width * 0.35;

    // 身体
    canvas.drawCircle(
      Offset(cx, cy),
      r,
      Paint()..color = const Color(0xFFE8A87C),
    );

    // 耳朵
    final earPath = Path()
      ..moveTo(cx - r * 0.6, cy - r * 0.5)
      ..lineTo(cx - r * 0.8, cy - r * 1.0)
      ..lineTo(cx - r * 0.3, cy - r * 0.7)
      ..moveTo(cx + r * 0.6, cy - r * 0.5)
      ..lineTo(cx + r * 0.8, cy - r * 1.0)
      ..lineTo(cx + r * 0.3, cy - r * 0.7);
    canvas.drawPath(
      earPath,
      Paint()
        ..color = const Color(0xFFE8A87C)
        ..style = PaintingStyle.fill,
    );

    // 眼睛
    final eyePaint = Paint()..color = const Color(0xFF3E2723);
    if (mood == PetMood.sleeping) {
      // 闭眼
      canvas.drawLine(
        Offset(cx - r * 0.3, cy - r * 0.1),
        Offset(cx - r * 0.15, cy - r * 0.1),
        eyePaint..strokeWidth = 2,
      );
      canvas.drawLine(
        Offset(cx + r * 0.15, cy - r * 0.1),
        Offset(cx + r * 0.3, cy - r * 0.1),
        eyePaint..strokeWidth = 2,
      );
    } else {
      canvas.drawCircle(
        Offset(cx - r * 0.22, cy - r * 0.1),
        r * 0.08,
        eyePaint,
      );
      canvas.drawCircle(
        Offset(cx + r * 0.22, cy - r * 0.1),
        r * 0.08,
        eyePaint,
      );
    }

    // 嘴巴
    final mouthPaint = Paint()
      ..color = const Color(0xFF3E2723)
      ..strokeWidth = 1.5
      ..style = PaintingStyle.stroke;
    if (mood == PetMood.talking) {
      canvas.drawOval(
        Rect.fromCenter(
          center: Offset(cx, cy + r * 0.2),
          width: 8,
          height: 6,
        ),
        Paint()..color = const Color(0xFFE53935),
      );
    } else if (mood == PetMood.happy) {
      canvas.drawArc(
        Rect.fromCenter(
          center: Offset(cx, cy + r * 0.15),
          width: 16,
          height: 10,
        ),
        0, pi, false, mouthPaint,
      );
    } else {
      // 简单微笑
      canvas.drawArc(
        Rect.fromCenter(
          center: Offset(cx - r * 0.08, cy + r * 0.15),
          width: 8,
          height: 6,
        ),
        0, pi, false, mouthPaint,
      );
      canvas.drawArc(
        Rect.fromCenter(
          center: Offset(cx + r * 0.08, cy + r * 0.15),
          width: 8,
          height: 6,
        ),
        0, pi, false, mouthPaint,
      );
    }
  }

  @override
  bool shouldRepaint(covariant _FallbackCatPainter oldDelegate) =>
      oldDelegate.mood != mood;
}
