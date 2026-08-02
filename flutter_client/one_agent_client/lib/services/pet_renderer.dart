import 'dart:math' as math;
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show rootBundle;
import 'package:flutter_live2d/flutter_live2d.dart';

/// 宠物情绪状态
enum PetMood {
  idle, // 待机
  talking, // 说话中（嘴动）
  thinking, // 思考中
  happy, // 开心
  sleeping, // 睡觉
}

/// 宠物渲染状态
@immutable
class PetRenderState {
  final PetMood mood;
  final double mouthOpen; // 0.0-1.0 嘴巴张开程度
  final double eyeOpen; // 0.0-1.0 眼睛睁开程度（0=闭眼）

  const PetRenderState({
    this.mood = PetMood.idle,
    this.mouthOpen = 0.0,
    this.eyeOpen = 1.0,
  });

  PetRenderState copyWith({
    PetMood? mood,
    double? mouthOpen,
    double? eyeOpen,
  }) =>
      PetRenderState(
        mood: mood ?? this.mood,
        mouthOpen: mouthOpen ?? this.mouthOpen,
        eyeOpen: eyeOpen ?? this.eyeOpen,
      );
}

/// 宠物渲染 Widget
///
/// 优先使用 Live2D 模型渲染（高质量动画 + 表情 + 嘴型同步）。
/// 若 assets/models/ 下无可用模型，自动 fallback 到 Canvas 自绘。
///
/// Live2D 参数映射（Cubism 标准参数）：
/// - ParamMouthOpenY: 嘴巴张开（0-1）← state.mouthOpen
/// - ParamEyeLOpen/ParamEyeROpen: 眼睛睁开（0-1）← state.eyeOpen
/// - ParamAngleX/Y/Z: 头部转动 ← 根据 mood 微调
/// - 表情切换：通过 setExpression(index)
class PetRenderer extends StatefulWidget {
  final PetRenderState state;
  final double size;
  final VoidCallback? onTap;

  /// Live2D 模型目录。支持 assets 路径（'assets/models/haru/'）
  /// 或文件系统绝对路径（'/data/.../live2d_models/haru/'）。
  /// 留空则自动检测 assets/models/ 下第一个 .model3.json。
  final String? modelDir;

  /// Live2D 模型文件名（如 'haru.model3.json'）。留空则自动检测。
  final String? modelFileName;

  const PetRenderer({
    super.key,
    this.state = const PetRenderState(),
    this.size = 120,
    this.onTap,
    this.modelDir,
    this.modelFileName,
  });

  @override
  State<PetRenderer> createState() => _PetRendererState();
}

class _PetRendererState extends State<PetRenderer>
    with TickerProviderStateMixin {
  // Live2D
  final _live2dController = Live2DViewController();
  bool _live2dAvailable = false;
  bool _live2dLoading = true;
  String? _live2dError;
  String? _resolvedModelDir;
  String? _resolvedModelFile;

  // Canvas fallback 动画
  late final AnimationController _breathController;
  late final AnimationController _blinkController;
  late final AnimationController _talkController;
  Duration _nextBlink = const Duration(seconds: 3);

  // 表情索引映射（不同模型差异较大，做容错）
  // 大多数 Live2D 模型前 5 个表情大致为：0=default,1=happy,2=angry,3=sad,4=surprised
  // 这里按 mood 做粗映射，找不到就忽略
  int _lastExpressionIndex = -1;

  @override
  void initState() {
    super.initState();

    // Canvas fallback 动画初始化
    _breathController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 3000),
    )..repeat(reverse: true);
    _blinkController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 120),
    );
    _talkController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 100),
    );
    _scheduleBlink();
    _updateTalkAnimation();

    // Live2D 初始化
    _initLive2D();
  }

  Future<void> _initLive2D() async {
    try {
      // 解析模型路径
      final (dir, file) = await _resolveModelPath();
      if (dir == null || file == null) {
        if (kDebugMode) {
          debugPrint('🐾 未找到 Live2D 模型，使用 Canvas fallback');
        }
        setState(() {
          _live2dAvailable = false;
          _live2dLoading = false;
          _live2dError = '未找到模型: $dir/$file';
        });
        return;
      }
      _resolvedModelDir = dir;
      _resolvedModelFile = file;

      // 等待 native view attached
      // ⚠️ 悬浮窗独立引擎首次创建时，PlatformView 注册和 surface 初始化可能较慢，
      // 给 15 秒超时（之前 5 秒太短，导致悬浮窗内 Live2D 永远加载不上）
      await _live2dController.whenAttached.timeout(
        const Duration(seconds: 15),
        onTimeout: () {
          if (kDebugMode) {
            debugPrint('⏰ Live2D whenAttached 超时（15s），使用 Canvas fallback');
          }
        },
      );
      if (!mounted) return;

      final ok = await _live2dController.loadModel(
        modelDir: dir,
        modelFileName: file,
      ).timeout(const Duration(seconds: 15), onTimeout: () {
        if (kDebugMode) debugPrint('⏰ Live2D loadModel 超时（15s）');
        return false;
      });
      if (!mounted) return;

      if (ok) {
        if (kDebugMode) debugPrint('✅ Live2D 模型加载成功: $dir/$file');
        setState(() {
          _live2dAvailable = true;
          _live2dLoading = false;
        });
        // 启动 idle 动作
        _startIdleMotion();
      } else {
        final err = _live2dController.value.lastError;
        if (kDebugMode) debugPrint('❌ Live2D 加载失败: ${err?.code} ${err?.message}');
        setState(() {
          _live2dAvailable = false;
          _live2dLoading = false;
          _live2dError = err?.message ?? 'Live2D 加载失败';
        });
      }
    } catch (e) {
      if (kDebugMode) debugPrint('❌ Live2D 初始化异常: $e');
      if (!mounted) return;
      setState(() {
        _live2dAvailable = false;
        _live2dLoading = false;
        _live2dError = e.toString();
      });
    }
  }

  /// 解析模型路径
  /// 优先级：modelDir（调用方指定，支持 assets 和文件系统）> 自动扫描 assets
  Future<(String?, String?)> _resolveModelPath() async {
    // 1. 调用方显式指定的路径（支持 assets 和文件系统绝对路径）
    if (widget.modelDir != null && widget.modelFileName != null) {
      return (widget.modelDir, widget.modelFileName);
    }
    // 2. 扫描 assets/models/ 下子目录（通过 AssetManifest.json）
    try {
      final manifestJson = await rootBundle.loadString('AssetManifest.json');
      final regex = RegExp(r'"([^"]+\.model3\.json)"');
      for (final m in regex.allMatches(manifestJson)) {
        final key = m.group(1)!;
        if (key.startsWith('assets/models/')) {
          final parts = key.split('/');
          final fileName = parts.last;
          final dir = parts.sublist(0, parts.length - 1).join('/') + '/';
          return (dir, fileName);
        }
      }
    } catch (e) {
      if (kDebugMode) debugPrint('AssetManifest.json 读取失败: $e');
    }
    return (null, null);
  }

  Future<void> _startIdleMotion() async {
    if (!_live2dAvailable) return;
    try {
      await _live2dController.startMotion(group: 'Idle', priority: 1);
    } catch (_) {
      try {
        await _live2dController.startMotion(group: 'Idle', index: 0, priority: 1);
      } catch (e) {
        if (kDebugMode) debugPrint('idle motion 启动失败: $e');
      }
    }
  }

  void _updateTalkAnimation() {
    if (widget.state.mood == PetMood.talking) {
      _talkController.repeat(reverse: true);
    } else {
      _talkController.stop();
    }
  }

  void _scheduleBlink() {
    Future.delayed(_nextBlink, () {
      if (!mounted) return;
      if (widget.state.mood != PetMood.sleeping) {
        _blinkController.forward().then((_) {
          _blinkController.reverse();
        });
      }
      _nextBlink = Duration(milliseconds: 2000 + math.Random().nextInt(3000));
      _scheduleBlink();
    });
  }

  @override
  void didUpdateWidget(PetRenderer oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.state.mood != widget.state.mood ||
        oldWidget.state.mouthOpen != widget.state.mouthOpen ||
        oldWidget.state.eyeOpen != widget.state.eyeOpen) {
      _updateTalkAnimation();
      _applyStateToLive2D(oldWidget.state);
    }
  }

  /// 把 PetRenderState 同步到 Live2D 模型（参数 + 表情 + 动作）
  void _applyStateToLive2D(PetRenderState oldState) {
    if (!_live2dAvailable) return;

    try {
      // 1. 嘴型同步（说话时）
      if (widget.state.mood == PetMood.talking) {
        _live2dController.setParameter(
            'ParamMouthOpenY', widget.state.mouthOpen);
        _live2dController.setParameter(
            'ParamMouthForm', widget.state.mouthOpen * 0.8 - 0.4);
      }

      // 2. 眼睛睁开
      if (widget.state.mood == PetMood.sleeping) {
        _live2dController.setParameter('ParamEyeLOpen', 0.0);
        _live2dController.setParameter('ParamEyeROpen', 0.0);
      } else {
        _live2dController.setParameter('ParamEyeLOpen', widget.state.eyeOpen);
        _live2dController.setParameter('ParamEyeROpen', widget.state.eyeOpen);
      }
    } catch (e) {
      if (kDebugMode) debugPrint('Live2D 参数设置失败: $e');
    }

    // 3. mood 变化时切换表情和动作
    if (oldState.mood != widget.state.mood) {
      _applyMoodChange(widget.state.mood);
    }
  }

  void _applyMoodChange(PetMood mood) {
    try {
      // 表情索引映射（不同模型差异大，容错处理）
      final exprIndex = _moodToExpressionIndex(mood);
      if (exprIndex >= 0 && exprIndex != _lastExpressionIndex) {
        _live2dController.setExpression(exprIndex);
        _lastExpressionIndex = exprIndex;
      }

      // 触发对应动作
      final motionGroup = _moodToMotionGroup(mood);
      if (motionGroup != null) {
        _live2dController.startMotion(group: motionGroup, priority: 2);
      }
    } catch (e) {
      if (kDebugMode) debugPrint('Live2D 表情/动作切换失败: $e');
    }
  }

  /// mood → 表情索引（粗映射，容错）
  int _moodToExpressionIndex(PetMood mood) {
    switch (mood) {
      case PetMood.idle:
        return -1; // 不切换，用默认
      case PetMood.talking:
        return -1; // 用参数驱动嘴型，不切表情
      case PetMood.thinking:
        return 6; // 常见模型第 7 个是 thinking
      case PetMood.happy:
        return 0; // 常见模型第 1 个是 happy/smile
      case PetMood.sleeping:
        return 4; // 常见模型第 5 个是 relaxed/sleepy
    }
  }

  /// mood → 动作组名（容错）
  String? _moodToMotionGroup(PetMood mood) {
    switch (mood) {
      case PetMood.idle:
        return 'Idle';
      case PetMood.talking:
        return 'TapBody'; // 点击身体动作可复用为说话
      case PetMood.thinking:
        return null; // 不切动作，让 idle 继续
      case PetMood.happy:
        return 'Tap'; // 点击动作通常带开心表情
      case PetMood.sleeping:
        return null; // 不切动作，靠闭眼参数
    }
  }

  @override
  void dispose() {
    _breathController.dispose();
    _blinkController.dispose();
    _talkController.dispose();
    _live2dController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    // ⚠️ 重要：Live2DView 必须始终被构建，且不能用 Opacity(0) 包裹，
    // 因为 Flutter PlatformView 在 opacity=0 时不会真正创建 native view，
    // 导致 _live2dController.whenAttached 永远卡住（死锁），一直转圈圈。
    //
    // 正确做法：底层始终放 Live2DView（完全不包裹 Opacity/Offstage），
    // 当 Live2D 不可用/加载中时，上层的 Canvas fallback 或 loading 容器
    // 会完全覆盖在 Live2DView 上面，达到"隐藏"效果，
    // 同时 native view 能正常 attached，whenAttached 能正常返回。

    final child = SizedBox(
      width: widget.size,
      height: widget.size,
      child: Stack(
        fit: StackFit.expand,
        children: [
          // 底层：Live2DView（始终直接构建，确保 native view attached）
          Live2DView(controller: _live2dController),
          // 中间层：Canvas fallback 覆盖层（Live2D 不可用或加载中时完全盖住底层）
          if (!_live2dAvailable || _live2dLoading)
            Positioned.fill(
              child: AnimatedBuilder(
                animation: Listenable.merge([
                  _breathController,
                  _blinkController,
                  _talkController,
                ]),
                builder: (context, _) {
                  return CustomPaint(
                    painter: _PetPainter(
                      state: widget.state,
                      breath: _breathController.value,
                      blink: _blinkController.value,
                      talk: _talkController.value,
                    ),
                  );
                },
              ),
            ),
          // 上层：加载中指示器（盖住 fallback 画的宠物）
          if (_live2dLoading)
            Positioned.fill(
              child: Container(
                color: Colors.white.withOpacity(0.5),
                alignment: Alignment.center,
                child: const SizedBox(
                  width: 24,
                  height: 24,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
              ),
            ),
          // 上层：错误提示（加载失败时）
          if (!_live2dLoading && !_live2dAvailable && _live2dError != null)
            Positioned(
              left: 4,
              right: 4,
              bottom: 4,
              child: Text(
                _live2dError!,
                style: const TextStyle(fontSize: 9, color: Colors.redAccent),
                textAlign: TextAlign.center,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
              ),
            ),
        ],
      ),
    );

    if (widget.onTap == null) return child;
    return GestureDetector(onTap: widget.onTap, child: child);
  }
}

/// Canvas fallback 绘制器（无 Live2D 模型时使用）
class _PetPainter extends CustomPainter {
  final PetRenderState state;
  final double breath;
  final double blink;
  final double talk;

  _PetPainter({
    required this.state,
    required this.breath,
    required this.blink,
    required this.talk,
  });

  @override
  void paint(Canvas canvas, Size size) {
    final center = Offset(size.width / 2, size.height / 2);
    final breathOffset = math.sin(breath * math.pi) * 3.0;
    final breathScale = 1.0 + math.sin(breath * math.pi) * 0.02;

    canvas.save();
    canvas.translate(0, -breathOffset);
    canvas.scale(breathScale);

    _drawBody(canvas, center, size);
    _drawEyes(canvas, center, size);
    _drawMouth(canvas, center, size);
    _drawBlush(canvas, center, size);
    _drawAccessory(canvas, center, size);

    canvas.restore();
  }

  void _drawBody(Canvas canvas, Offset center, Size size) {
    final radius = size.width * 0.38;
    final bodyPaint = Paint()
      ..shader = RadialGradient(
        colors: _bodyColors(),
        center: const Alignment(-0.3, -0.3),
        radius: 1.0,
      ).createShader(Rect.fromCircle(center: center, radius: radius));
    canvas.drawCircle(center, radius, bodyPaint);

    final highlightPaint = Paint()
      ..color = Colors.white.withOpacity(0.3)
      ..style = PaintingStyle.fill;
    canvas.drawCircle(
      Offset(center.dx - radius * 0.35, center.dy - radius * 0.4),
      radius * 0.2,
      highlightPaint,
    );
  }

  List<Color> _bodyColors() {
    switch (state.mood) {
      case PetMood.happy:
        return [const Color(0xFFFFD54F), const Color(0xFFFFB300)];
      case PetMood.sleeping:
        return [const Color(0xFFB0BEC5), const Color(0xFF78909C)];
      case PetMood.thinking:
        return [const Color(0xFF90CAF9), const Color(0xFF42A5F5)];
      case PetMood.talking:
        return [const Color(0xFFCE93D8), const Color(0xFFAB47BC)];
      case PetMood.idle:
        return [const Color(0xFF81C784), const Color(0xFF43A047)];
    }
  }

  void _drawEyes(Canvas canvas, Offset center, Size size) {
    final radius = size.width * 0.38;
    final eyeOffsetX = radius * 0.32;
    final eyeY = center.dy - radius * 0.1;
    final eyeWidth = radius * 0.18;
    final eyeOpenFactor = state.mood == PetMood.sleeping
        ? 0.0
        : state.eyeOpen * (1.0 - blink);
    final eyeHeight = eyeWidth * 0.5 * eyeOpenFactor;

    final eyePaint = Paint()
      ..color = const Color(0xFF2E2E2E)
      ..style = PaintingStyle.fill;

    if (eyeOpenFactor < 0.1) {
      final linePaint = Paint()
        ..color = const Color(0xFF2E2E2E)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 2.5
        ..strokeCap = StrokeCap.round;
      canvas.drawArc(
        Rect.fromCenter(
          center: Offset(center.dx - eyeOffsetX, eyeY),
          width: eyeWidth * 1.2,
          height: eyeWidth * 0.6,
        ),
        math.pi, math.pi, false, linePaint,
      );
      canvas.drawArc(
        Rect.fromCenter(
          center: Offset(center.dx + eyeOffsetX, eyeY),
          width: eyeWidth * 1.2,
          height: eyeWidth * 0.6,
        ),
        math.pi, math.pi, false, linePaint,
      );
    } else {
      canvas.drawOval(
        Rect.fromCenter(
          center: Offset(center.dx - eyeOffsetX, eyeY),
          width: eyeWidth,
          height: eyeHeight,
        ),
        eyePaint,
      );
      canvas.drawOval(
        Rect.fromCenter(
          center: Offset(center.dx + eyeOffsetX, eyeY),
          width: eyeWidth,
          height: eyeHeight,
        ),
        eyePaint,
      );
      final highlightPaint = Paint()..color = Colors.white;
      canvas.drawCircle(
        Offset(center.dx - eyeOffsetX + eyeWidth * 0.2,
            eyeY - eyeHeight * 0.3),
        eyeWidth * 0.12,
        highlightPaint,
      );
      canvas.drawCircle(
        Offset(center.dx + eyeOffsetX + eyeWidth * 0.2,
            eyeY - eyeHeight * 0.3),
        eyeWidth * 0.12,
        highlightPaint,
      );
    }
  }

  void _drawMouth(Canvas canvas, Offset center, Size size) {
    final radius = size.width * 0.38;
    final mouthY = center.dy + radius * 0.2;
    final mouthPaint = Paint()
      ..color = const Color(0xFF2E2E2E)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2.5
      ..strokeCap = StrokeCap.round;

    final mouthOpen = state.mood == PetMood.talking
        ? state.mouthOpen * (0.3 + talk * 0.7)
        : state.mouthOpen;

    switch (state.mood) {
      case PetMood.talking:
        final mouthWidth = radius * 0.25;
        final mouthHeight = mouthWidth * (0.3 + mouthOpen * 0.7);
        final mouthPaintFill = Paint()
          ..color = const Color(0xFFD32F2F)
          ..style = PaintingStyle.fill;
        canvas.drawOval(
          Rect.fromCenter(
            center: Offset(center.dx, mouthY),
            width: mouthWidth,
            height: mouthHeight,
          ),
          mouthPaintFill,
        );
        break;
      case PetMood.happy:
        canvas.drawArc(
          Rect.fromCenter(
            center: Offset(center.dx, mouthY - radius * 0.05),
            width: radius * 0.4,
            height: radius * 0.3,
          ),
          0, math.pi, false, mouthPaint,
        );
        break;
      case PetMood.sleeping:
        canvas.drawArc(
          Rect.fromCenter(
            center: Offset(center.dx, mouthY),
            width: radius * 0.15,
            height: radius * 0.1,
          ),
          0, math.pi, false, mouthPaint,
        );
        break;
      case PetMood.thinking:
        canvas.drawLine(
          Offset(center.dx - radius * 0.1, mouthY),
          Offset(center.dx + radius * 0.12, mouthY - radius * 0.05),
          mouthPaint,
        );
        break;
      case PetMood.idle:
        canvas.drawArc(
          Rect.fromCenter(
            center: Offset(center.dx, mouthY),
            width: radius * 0.25,
            height: radius * 0.15,
          ),
          0.2, math.pi - 0.4, false, mouthPaint,
        );
        break;
    }
  }

  void _drawBlush(Canvas canvas, Offset center, Size size) {
    final radius = size.width * 0.38;
    final blushOffsetX = radius * 0.55;
    final blushY = center.dy + radius * 0.05;
    final blushRadius = radius * 0.1;
    final blushPaint = Paint()
      ..color = const Color(0xFFFF8A80).withOpacity(0.5)
      ..style = PaintingStyle.fill;
    canvas.drawCircle(Offset(center.dx - blushOffsetX, blushY), blushRadius, blushPaint);
    canvas.drawCircle(Offset(center.dx + blushOffsetX, blushY), blushRadius, blushPaint);
  }

  void _drawAccessory(Canvas canvas, Offset center, Size size) {
    final radius = size.width * 0.38;
    switch (state.mood) {
      case PetMood.thinking:
        final bubbleY = center.dy - radius * 1.4;
        final bubblePaint = Paint()
          ..color = Colors.white.withOpacity(0.8)
          ..style = PaintingStyle.fill;
        canvas.drawCircle(
          Offset(center.dx + radius * 0.5, bubbleY + radius * 0.3),
          radius * 0.08,
          bubblePaint,
        );
        canvas.drawCircle(
          Offset(center.dx + radius * 0.6, bubbleY + radius * 0.1),
          radius * 0.13,
          bubblePaint,
        );
        final textPainter = TextPainter(
          text: TextSpan(
            text: '?',
            style: TextStyle(
              color: Colors.black54,
              fontSize: radius * 0.25,
              fontWeight: FontWeight.bold,
            ),
          ),
          textDirection: TextDirection.ltr,
        )..layout();
        textPainter.paint(
          canvas,
          Offset(
            center.dx + radius * 0.6 - textPainter.width / 2,
            bubbleY + radius * 0.1 - textPainter.height / 2,
          ),
        );
        break;
      case PetMood.sleeping:
        for (int i = 0; i < 3; i++) {
          final zzzY = center.dy - radius * 1.2 - i * radius * 0.3;
          final fontSize = radius * (0.15 + i * 0.05);
          final tp = TextPainter(
            text: TextSpan(
              text: 'Z',
              style: TextStyle(
                color: const Color(0xFF42A5F5).withOpacity(0.4 + i * 0.2),
                fontSize: fontSize,
                fontWeight: FontWeight.bold,
              ),
            ),
            textDirection: TextDirection.ltr,
          )..layout();
          tp.paint(
            canvas,
            Offset(center.dx + radius * 0.4 + i * radius * 0.2, zzzY),
          );
        }
        break;
      default:
        break;
    }
  }

  @override
  bool shouldRepaint(covariant _PetPainter oldDelegate) => true;
}
