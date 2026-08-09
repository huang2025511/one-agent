import 'dart:math' as math;
import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show rootBundle;
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
  Artboard? _artboard;
  StateMachineController? _controller;
  SMIInput<bool>? _idleInput;
  SMIInput<bool>? _talkInput;
  SMIInput<bool>? _happyInput;
  SMIInput<bool>? _sleepInput;
  SMIInput<bool>? _thinkInput;

  bool _loadFailed = false;

  @override
  void initState() {
    super.initState();
    _loadRive();
  }

  @override
  void didUpdateWidget(PetWidget oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.mood != widget.mood || oldWidget.action != widget.action) {
      _updateAnimation();
    }
  }

  @override
  void dispose() {
    _controller?.dispose();
    super.dispose();
  }

  void _loadRive() async {
    try {
      final data = await rootBundle.load('assets/rive/bouncy_cat.riv');
      final file = RiveFile.import(data);
      final artboard = file.mainArtboard;

      // 尝试找到状态机
      for (final smc in StateMachineController.extend(artboard)) {
        _controller = smc;
        artboard.addController(smc);
        break;
      }

      if (_controller != null) {
        _idleInput = _controller!.findInput<bool>('idle');
        _talkInput = _controller!.findInput<bool>('talk');
        _happyInput = _controller!.findInput<bool>('happy');
        _sleepInput = _controller!.findInput<bool>('sleep');
        _thinkInput = _controller!.findInput<bool>('think');
      }

      if (mounted) {
        setState(() {
          _artboard = artboard;
        });
        _updateAnimation();
      }
    } catch (e) {
      debugPrint('Rive 加载失败，使用 Canvas fallback: $e');
      if (mounted) {
        setState(() => _loadFailed = true);
      }
    }
  }

  void _updateAnimation() {
    if (_controller == null) return;

    _idleInput?.value = false;
    _talkInput?.value = false;
    _happyInput?.value = false;
    _sleepInput?.value = false;
    _thinkInput?.value = false;

    switch (widget.action) {
      case PetAction.idle:
      case PetAction.blink:
      case PetAction.lookAround:
      case PetAction.tilt:
        _idleInput?.value = true;
        break;
      case PetAction.bounce:
      case PetAction.wave:
      case PetAction.happy:
        _happyInput?.value = true;
        break;
      case PetAction.sleep:
        _sleepInput?.value = true;
        break;
      case PetAction.talk:
        _talkInput?.value = true;
        break;
      case PetAction.think:
        _thinkInput?.value = true;
        break;
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loadFailed) {
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
        0, math.pi, false, mouthPaint,
      );
    } else {
      canvas.drawArc(
        Rect.fromCenter(
          center: Offset(cx - r * 0.08, cy + r * 0.15),
          width: 8,
          height: 6,
        ),
        0, math.pi, false, mouthPaint,
      );
      canvas.drawArc(
        Rect.fromCenter(
          center: Offset(cx + r * 0.08, cy + r * 0.15),
          width: 8,
          height: 6,
        ),
        0, math.pi, false, mouthPaint,
      );
    }
  }

  @override
  bool shouldRepaint(covariant _FallbackCatPainter oldDelegate) =>
      oldDelegate.mood != mood;
}
