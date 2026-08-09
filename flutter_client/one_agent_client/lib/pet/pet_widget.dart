import 'dart:math' as math;
import 'package:flutter/material.dart';

import 'pet_brain.dart';

/// 宠物渲染 Widget — 纯 Canvas 动画实现
///
/// 不依赖外部资源（Rive/Live2D），确保在悬浮窗中稳定渲染。
/// 通过 [AnimationController] 驱动呼吸、眨眼、动作等动画，
/// 让宠物真正"自然运动起来"。
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

class _PetWidgetState extends State<PetWidget>
    with TickerProviderStateMixin {
  // 主时钟：驱动所有连续动画（呼吸、摆动、说话嘴型等）
  late final AnimationController _tick;

  // 眨眼控制器：单次短促动画
  late final AnimationController _blink;

  // 当前动作的瞬时动画（如 bounce 弹跳一次）
  late final AnimationController _actionAnim;

  // 上次的 action，用于检测动作切换并触发一次性动画
  PetAction? _lastAction;

  // 眨眼调度
  int _nextBlinkAt = 0;

  @override
  void initState() {
    super.initState();
    _tick = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 2),
    )..repeat();

    _blink = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 150),
    );

    _actionAnim = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 800),
    );

    _tick.addListener(() {
      if (mounted) setState(() {});
      // 简易眨眼调度
      final now = DateTime.now().millisecondsSinceEpoch;
      if (now >= _nextBlinkAt) {
        _blink.forward(from: 0).then((_) {
          // 短暂闭眼后睁开
          Future.delayed(const Duration(milliseconds: 60), () {
            _blink.reverse();
          });
        });
        // 下次眨眼：2.5~6 秒后
        _nextBlinkAt = now + 2500 + math.Random().nextInt(3500);
      }
    });

    _lastAction = widget.action;
    // 动作切换时触发一次性动画
    _maybeFireActionAnim(widget.action);
  }

  @override
  void didUpdateWidget(PetWidget oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.action != widget.action) {
      _lastAction = widget.action;
      _maybeFireActionAnim(widget.action);
    }
  }

  void _maybeFireActionAnim(PetAction action) {
    switch (action) {
      case PetAction.bounce:
      case PetAction.wave:
      case PetAction.happy:
        _actionAnim.forward(from: 0);
        break;
      case PetAction.tilt:
        _actionAnim.forward(from: 0);
        break;
      default:
        break;
    }
  }

  @override
  void dispose() {
    _tick.dispose();
    _blink.dispose();
    _actionAnim.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: widget.size,
      height: widget.size,
      child: CustomPaint(
        painter: _PetPainter(
          mood: widget.mood,
          action: widget.action,
          tick: _tick.value,
          blink: 1.0 - _blink.value, // 1=睁眼, 0=闭眼
          actionAnim: _actionAnim.value,
        ),
      ),
    );
  }
}

/// 宠物 Canvas 画笔
class _PetPainter extends CustomPainter {
  final PetMood mood;
  final PetAction action;
  final double tick;       // 0..1 循环
  final double blink;      // 0..1, 1=睁眼
  final double actionAnim; // 0..1 一次性动作进度

  _PetPainter({
    required this.mood,
    required this.action,
    required this.tick,
    required this.blink,
    required this.actionAnim,
  });

  // 主色
  static const Color _bodyColor = Color(0xFFE8A87C);
  static const Color _bodyDark = Color(0xFFD88E63);
  static const Color _bellyColor = Color(0xFFF5D5B8);
  static const Color _earInner = Color(0xFFF2A89C);
  static const Color _eyeColor = Color(0xFF3E2723);
  static const Color _noseColor = Color(0xFFE53935);
  static const Color _blushColor = Color(0xFFFF8A80);

  @override
  void paint(Canvas canvas, Size size) {
    final cx = size.width / 2;
    final cy = size.height / 2;
    final r = size.width * 0.32;

    // ── 1. 计算各种动画参数 ──────────────────────────────
    final t = tick * math.pi * 2; // 0..2π

    // 呼吸（待机常态）
    double breatheY = math.sin(t) * 2.5;
    double breatheScale = 1.0 + math.sin(t) * 0.015;

    // 说话：嘴型 + 轻微头部摇晃
    double mouthOpen = 0;
    double headTilt = 0;
    double bodySwing = 0;

    if (mood == PetMood.talking || action == PetAction.talk) {
      mouthOpen = (math.sin(t * 6) * 0.5 + 0.5) * 6 + 1;
      headTilt = math.sin(t * 3) * 0.04;
    }

    // 思考：头部倾斜
    if (mood == PetMood.thinking || action == PetAction.think) {
      headTilt = -0.15 + math.sin(t * 0.8) * 0.03;
    }

    // 开心：弹跳
    double bounceY = 0;
    if (action == PetAction.bounce || action == PetAction.happy ||
        mood == PetMood.happy || mood == PetMood.excited) {
      // actionAnim 控制单次弹跳；同时叠加一个轻微的呼吸抖动
      final bp = actionAnim;
      // 抛物线弹跳：y = -4*p*(1-p) (峰值在 p=0.5)
      bounceY = -4 * bp * (1 - bp) * 18;
      breatheScale = 1.0 + math.sin(t * 4) * 0.02;
    }

    // 睡觉：缓慢呼吸 + 上下浮动更大
    if (mood == PetMood.sleeping || action == PetAction.sleep) {
      breatheY = math.sin(t * 0.5) * 4;
      breatheScale = 1.0 + math.sin(t * 0.5) * 0.04;
    }

    // 好奇：左右看
    double lookX = 0;
    if (action == PetAction.lookAround || mood == PetMood.curious) {
      lookX = math.sin(t * 1.2) * 0.18;
    }

    // 歪头
    if (action == PetAction.tilt) {
      headTilt = (actionAnim - 0.5) * 0.4;
    }

    // 挥手：手部摆动
    double waveAngle = 0;
    if (action == PetAction.wave) {
      waveAngle = math.sin(actionAnim * math.pi * 4) * 0.5;
    }

    // 整体浮动（轻微上下）
    final floatY = math.sin(t * 0.7) * 1.5;

    // ── 2. 绘制阴影 ──────────────────────────────────────
    final shadowPaint = Paint()
      ..color = Colors.black.withOpacity(0.12)
      ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 4);
    final shadowOffset = Offset(cx, cy + r * 0.95);
    canvas.drawOval(
      Rect.fromCenter(
        center: shadowOffset,
        width: r * 1.4,
        height: r * 0.35,
      ),
      shadowPaint,
    );

    // ── 3. 绘制身体（含呼吸/弹跳） ───────────────────────
    canvas.save();
    canvas.translate(cx, cy + breatheY + bounceY + floatY);
    canvas.scale(breatheScale, breatheScale);

    // 尾巴（在身体后面绘制）
    _drawTail(canvas, r, t, mood, action);

    // 身体（圆形主体 + 肚子）
    _drawBody(canvas, r, bodySwing);

    // 耳朵
    _drawEars(canvas, r, headTilt);

    // 头部（脸 + 眼睛 + 鼻子 + 嘴）
    canvas.save();
    canvas.translate(0, -r * 0.05);
    canvas.rotate(headTilt);
    _drawFace(canvas, r, lookX, blink, mouthOpen, mood, action);
    canvas.restore();

    // 手臂（用于挥手）
    _drawArms(canvas, r, waveAngle, action);

    canvas.restore();

    // ── 4. 状态特效（思考点、睡觉 Zzz、爱心等） ──────────
    _drawEffects(canvas, cx, cy + breatheY + bounceY + floatY, r, mood, action, t);
  }

  void _drawTail(Canvas canvas, double r, double t, PetMood mood, PetAction action) {
    // 尾巴摇摆
    double tailAngle = math.sin(t * 1.5) * 0.3;
    if (mood == PetMood.happy || mood == PetMood.excited) {
      tailAngle = math.sin(t * 4) * 0.5; // 开心时摇得更快
    }

    final tailPaint = Paint()
      ..color = _bodyColor
      ..style = PaintingStyle.fill
      ..strokeCap = StrokeCap.round;

    final path = Path();
    final baseX = r * 0.55;
    final baseY = r * 0.1;
    path.moveTo(baseX, baseY);
    // 尾巴呈曲线
    final tipX = baseX + r * 0.5 * math.cos(tailAngle - 0.3);
    final tipY = baseY - r * 0.6 * math.sin(tailAngle + 0.5);
    final ctrlX = baseX + r * 0.55;
    final ctrlY = baseY - r * 0.3;
    path.quadraticBezierTo(ctrlX, ctrlY, tipX, tipY);
    path.quadraticBezierTo(ctrlX - r * 0.1, ctrlY, baseX, baseY);

    canvas.drawPath(path, tailPaint..strokeWidth = r * 0.18);
  }

  void _drawBody(Canvas canvas, double r, double swing) {
    // 主体（圆形）
    final bodyPaint = Paint()
      ..color = _bodyColor
      ..style = PaintingStyle.fill;
    canvas.drawCircle(Offset.zero, r, bodyPaint);

    // 肚子（浅色椭圆）
    final bellyPaint = Paint()..color = _bellyColor;
    canvas.drawOval(
      Rect.fromCenter(
        center: Offset(0, r * 0.2),
        width: r * 1.1,
        height: r * 1.2,
      ),
      bellyPaint,
    );

    // 身体边缘描边（深色，增加层次）
    final outlinePaint = Paint()
      ..color = _bodyDark
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.2;
    canvas.drawCircle(Offset.zero, r, outlinePaint);
  }

  void _drawEars(Canvas canvas, double r, double tilt) {
    final earPaint = Paint()..color = _bodyColor;
    final earInnerPaint = Paint()..color = _earInner;

    // 左耳
    canvas.save();
    canvas.translate(-r * 0.55, -r * 0.7);
    canvas.rotate(-0.3 + tilt * 0.5);
    final leftEar = Path()
      ..moveTo(0, 0)
      ..lineTo(-r * 0.25, -r * 0.45)
      ..lineTo(r * 0.15, -r * 0.1)
      ..close();
    canvas.drawPath(leftEar, earPaint);
    canvas.drawPath(leftEar, Paint()..color = _bodyDark..style = PaintingStyle.stroke..strokeWidth = 1);
    // 内耳
    final leftInner = Path()
      ..moveTo(-r * 0.05, -r * 0.05)
      ..lineTo(-r * 0.15, -r * 0.3)
      ..lineTo(r * 0.05, -r * 0.1)
      ..close();
    canvas.drawPath(leftInner, earInnerPaint);
    canvas.restore();

    // 右耳
    canvas.save();
    canvas.translate(r * 0.55, -r * 0.7);
    canvas.rotate(0.3 - tilt * 0.5);
    final rightEar = Path()
      ..moveTo(0, 0)
      ..lineTo(r * 0.25, -r * 0.45)
      ..lineTo(-r * 0.15, -r * 0.1)
      ..close();
    canvas.drawPath(rightEar, earPaint);
    canvas.drawPath(rightEar, Paint()..color = _bodyDark..style = PaintingStyle.stroke..strokeWidth = 1);
    final rightInner = Path()
      ..moveTo(r * 0.05, -r * 0.05)
      ..lineTo(r * 0.15, -r * 0.3)
      ..lineTo(-r * 0.05, -r * 0.1)
      ..close();
    canvas.drawPath(rightInner, earInnerPaint);
    canvas.restore();
  }

  void _drawFace(Canvas canvas, double r, double lookX, double blinkV,
      double mouthOpen, PetMood mood, PetAction action) {
    final eyePaint = Paint()..color = _eyeColor;
    final isSleeping = mood == PetMood.sleeping || action == PetAction.sleep;
    final isHappy = mood == PetMood.happy || action == PetAction.happy;
    final isSad = mood == PetMood.sad;
    final isExcited = mood == PetMood.excited;

    // ── 眼睛 ──
    final eyeY = -r * 0.05;
    final eyeOffsetX = r * 0.22;
    final eyeR = r * 0.09;

    if (isSleeping) {
      // 闭眼（弧线）
      final closedPaint = Paint()
        ..color = _eyeColor
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2;
      for (final sign in [-1, 1]) {
        final x = sign * eyeOffsetX;
        canvas.drawArc(
          Rect.fromCenter(center: Offset(x + lookX * r, eyeY), width: eyeR * 2, height: eyeR),
          0, math.pi, false, closedPaint,
        );
      }
    } else if (isHappy) {
      // 弯弯眼（^ ^）
      final happyPaint = Paint()
        ..color = _eyeColor
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2.5
        ..strokeCap = StrokeCap.round;
      for (final sign in [-1, 1]) {
        final x = sign * eyeOffsetX;
        final path = Path()
          ..moveTo(x - eyeR + lookX * r, eyeY + eyeR * 0.3)
          ..quadraticBezierTo(x + lookX * r, eyeY - eyeR * 0.5, x + eyeR + lookX * r, eyeY + eyeR * 0.3);
        canvas.drawPath(path, happyPaint);
      }
    } else {
      // 正常眼睛（带眨眼）—— 椭圆，纵向高度受 blink 影响
      final openH = eyeR * 2 * (0.15 + blinkV * 0.85);
      for (final sign in [-1, 1]) {
        final x = sign * eyeOffsetX + lookX * r;
        // 眼白
        canvas.drawOval(
          Rect.fromCenter(center: Offset(x, eyeY), width: eyeR * 2.2, height: openH),
          Paint()..color = Colors.white,
        );
        // 瞳孔
        if (blinkV > 0.3) {
          canvas.drawOval(
            Rect.fromCenter(center: Offset(x, eyeY), width: eyeR * 1.6, height: openH * 0.85),
            eyePaint,
          );
          // 高光
          if (blinkV > 0.6) {
            canvas.drawCircle(
              Offset(x - eyeR * 0.3, eyeY - eyeR * 0.3),
              eyeR * 0.25,
              Paint()..color = Colors.white,
            );
          }
        }
      }
    }

    // ── 腮红（开心/兴奋时） ──
    if (isHappy || isExcited) {
      final blushPaint = Paint()..color = _blushColor.withOpacity(0.6);
      for (final sign in [-1, 1]) {
        canvas.drawOval(
          Rect.fromCenter(
            center: Offset(sign * r * 0.45, r * 0.18),
            width: r * 0.22,
            height: r * 0.12,
          ),
          blushPaint,
        );
      }
    }

    // ── 鼻子 ──
    final noseY = r * 0.15;
    final nosePath = Path()
      ..moveTo(-r * 0.06, noseY)
      ..quadraticBezierTo(0, noseY - r * 0.04, r * 0.06, noseY)
      ..quadraticBezierTo(r * 0.03, noseY + r * 0.05, 0, noseY + r * 0.05)
      ..quadraticBezierTo(-r * 0.03, noseY + r * 0.05, -r * 0.06, noseY)
      ..close();
    canvas.drawPath(nosePath, Paint()..color = _noseColor);

    // ── 嘴巴 ──
    final mouthY = r * 0.3;
    final mouthPaint = Paint()
      ..color = _eyeColor
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2
      ..strokeCap = StrokeCap.round;

    if (mouthOpen > 0.5) {
      // 说话：张嘴
      canvas.drawOval(
        Rect.fromCenter(
          center: Offset(0, mouthY),
          width: r * 0.25,
          height: mouthOpen,
        ),
        Paint()..color = const Color(0xFF8B4513),
      );
      // 舌头
      canvas.drawOval(
        Rect.fromCenter(
          center: Offset(0, mouthY + mouthOpen * 0.2),
          width: r * 0.15,
          height: mouthOpen * 0.5,
        ),
        Paint()..color = const Color(0xFFEF5350),
      );
    } else if (isHappy) {
      // 大笑
      final path = Path()
        ..moveTo(-r * 0.18, mouthY - r * 0.05)
        ..quadraticBezierTo(0, mouthY + r * 0.2, r * 0.18, mouthY - r * 0.05);
      canvas.drawPath(path, mouthPaint);
    } else if (isSad) {
      // 难过：倒弧
      final path = Path()
        ..moveTo(-r * 0.12, mouthY + r * 0.05)
        ..quadraticBezierTo(0, mouthY - r * 0.08, r * 0.12, mouthY + r * 0.05);
      canvas.drawPath(path, mouthPaint);
    } else if (isSleeping) {
      // 睡觉：小 O
      canvas.drawOval(
        Rect.fromCenter(center: Offset(0, mouthY), width: r * 0.1, height: r * 0.08),
        mouthPaint,
      );
    } else {
      // 默认：微笑
      final path = Path()
        ..moveTo(-r * 0.12, mouthY)
        ..quadraticBezierTo(0, mouthY + r * 0.1, r * 0.12, mouthY);
      canvas.drawPath(path, mouthPaint);
    }
  }

  void _drawArms(Canvas canvas, double r, double waveAngle, PetAction action) {
    final armPaint = Paint()
      ..color = _bodyColor
      ..style = PaintingStyle.fill
      ..strokeCap = StrokeCap.round;

    // 左手（默认放身体旁）
    canvas.save();
    canvas.translate(-r * 0.7, r * 0.1);
    canvas.rotate(0.3);
    canvas.drawRRect(
      RRect.fromRectAndRadius(
        Rect.fromCenter(center: Offset.zero, width: r * 0.2, height: r * 0.5),
        Radius.circular(r * 0.1),
      ),
      armPaint,
    );
    canvas.restore();

    // 右手（挥手时举起摆动）
    canvas.save();
    canvas.translate(r * 0.7, r * 0.1);
    if (action == PetAction.wave) {
      // 举起手并摆动
      canvas.translate(r * 0.2, -r * 0.5);
      canvas.rotate(-1.2 + waveAngle);
    } else {
      canvas.rotate(-0.3);
    }
    canvas.drawRRect(
      RRect.fromRectAndRadius(
        Rect.fromCenter(center: Offset.zero, width: r * 0.2, height: r * 0.5),
        Radius.circular(r * 0.1),
      ),
      armPaint,
    );
    canvas.restore();
  }

  void _drawEffects(Canvas canvas, double cx, double cy, double r,
      PetMood mood, PetAction action, double t) {
    // 思考：上方思考气泡（...）
    if (mood == PetMood.thinking || action == PetAction.think) {
      final dotPaint = Paint()..color = Colors.white.withOpacity(0.9);
      for (int i = 0; i < 3; i++) {
        final phase = (t * 2 + i * 0.5) % 1.0;
        final dy = -r * 1.3 - i * r * 0.18;
        final dx = r * 0.5 + i * r * 0.12;
        final scale = 0.5 + phase * 0.5;
        canvas.drawCircle(
          Offset(cx + dx, cy + dy),
          r * 0.08 * scale,
          dotPaint,
        );
      }
    }

    // 睡觉：Zzz
    if (mood == PetMood.sleeping || action == PetAction.sleep) {
      final textPainter = TextPainter(
        textDirection: TextDirection.ltr,
      );
      for (int i = 0; i < 3; i++) {
        final phase = (t * 0.3 + i * 0.33) % 1.0;
        final dy = -r * 1.0 - phase * r * 0.8;
        final dx = r * 0.6 + phase * r * 0.5;
        final opacity = (1 - phase).clamp(0.0, 1.0);
        final size = 12.0 + i * 4;
        textPainter.text = TextSpan(
          text: 'Z',
          style: TextStyle(
            color: Colors.white.withOpacity(opacity * 0.9),
            fontSize: size,
            fontWeight: FontWeight.bold,
          ),
        );
        textPainter.layout();
        textPainter.paint(canvas, Offset(cx + dx, cy + dy));
      }
    }

    // 开心/兴奋：爱心粒子
    if (mood == PetMood.excited || (action == PetAction.happy && mood == PetMood.happy)) {
      final heartPaint = Paint()..color = const Color(0xFFE91E63);
      for (int i = 0; i < 3; i++) {
        final phase = (t + i * 0.33) % 1.0;
        final dy = -r * 0.8 - phase * r * 1.0;
        final dx = (i - 1) * r * 0.4;
        final opacity = (1 - phase).clamp(0.0, 1.0);
        final scale = (0.3 + phase * 0.5).clamp(0.0, 1.0);
        _drawHeart(canvas, Offset(cx + dx, cy + dy), r * 0.12 * scale, heartPaint..color = heartPaint.color.withOpacity(opacity));
      }
    }
  }

  void _drawHeart(Canvas canvas, Offset center, double size, Paint paint) {
    final path = Path();
    path.moveTo(center.dx, center.dy + size * 0.3);
    path.cubicTo(
      center.dx, center.dy,
      center.dx - size, center.dy,
      center.dx - size, center.dy - size * 0.3,
    );
    path.cubicTo(
      center.dx - size, center.dy - size * 0.8,
      center.dx, center.dy - size * 0.8,
      center.dx, center.dy - size * 0.3,
    );
    path.cubicTo(
      center.dx, center.dy - size * 0.8,
      center.dx + size, center.dy - size * 0.8,
      center.dx + size, center.dy - size * 0.3,
    );
    path.cubicTo(
      center.dx + size, center.dy,
      center.dx, center.dy,
      center.dx, center.dy + size * 0.3,
    );
    canvas.drawPath(path, paint);
  }

  @override
  bool shouldRepaint(covariant _PetPainter oldDelegate) =>
      oldDelegate.tick != tick ||
      oldDelegate.blink != blink ||
      oldDelegate.actionAnim != actionAnim ||
      oldDelegate.mood != mood ||
      oldDelegate.action != action;
}
