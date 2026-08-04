import 'dart:async';
import 'dart:convert';
import 'dart:math';
import 'package:flutter/material.dart';
import 'package:flutter_overlay_window/flutter_overlay_window.dart';

import '../api/api_client.dart';
import '../api/chat_api.dart';
import '../api/sse_client.dart';
import '../models/chat_message.dart';

/// ⚠️ 悬浮窗入口 overlayMain() 已在 main.dart 中统一定义（避免重复符号冲突）
///
/// 悬浮窗内的桌宠页面
///
/// 由于 Android 悬浮窗运行在 Service 中，PlatformView（Live2D）无法使用，
/// 因此使用 Canvas 绘制一个精致的猫角色，具备：
/// - 猫耳、猫脸、猫须、猫尾巴
/// - 呼吸、眨眼、尾巴摆动、走路等动画
/// - 4种表情（idle/talking/thinking/happy）
/// - 气泡消息显示
/// - 文字聊天输入

class OverlayPetScreen extends StatefulWidget {
  const OverlayPetScreen({super.key});

  @override
  State<OverlayPetScreen> createState() => _OverlayPetScreenState();
}

enum _PetMood { idle, thinking, talking, happy }

class _OverlayPetScreenState extends State<OverlayPetScreen>
    with TickerProviderStateMixin {
  // 配置
  String _baseUrl = 'http://127.0.0.1:18792';
  String _apiKey = '';
  String? _sessionId;

  // 宠物状态
  _PetMood _mood = _PetMood.idle;

  // 动画控制器
  late final AnimationController _breathController; // 呼吸
  late final AnimationController _tailController;   // 尾巴摆动
  late final AnimationController _blinkController;  // 眨眼
  late final AnimationController _walkController;   // 走路/身体微动
  Timer? _blinkTimer;

  // 聊天状态
  String _bubbleText = '';
  bool _isThinking = false;
  final StringBuffer _replyBuffer = StringBuffer();

  // SSE
  StreamSubscription<StreamEvent>? _streamSub;
  SseClient? _sseClient;

  // 输入框
  final TextEditingController _inputController = TextEditingController();
  bool _showInput = false;

  // 主 APP 消息监听
  StreamSubscription? _mainAppSub;

  @override
  void initState() {
    super.initState();

    _breathController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 3000),
    )..repeat(reverse: true);

    _tailController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1500),
    )..repeat(reverse: true);

    _blinkController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 120),
    );

    _walkController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 2000),
    )..repeat();

    _scheduleBlink();

    _mainAppSub = FlutterOverlayWindow.overlayListener.listen((event) {
      _handleMainAppMessage(event.toString());
    });
  }

  void _scheduleBlink() {
    _blinkTimer?.cancel();
    final delay = Duration(seconds: 2 + Random().nextInt(4));
    _blinkTimer = Timer(delay, () {
      if (mounted) {
        _blinkController.forward().then((_) {
          _blinkController.reverse();
        });
        _scheduleBlink();
      }
    });
  }

  @override
  void dispose() {
    _streamSub?.cancel();
    _sseClient?.dispose();
    _mainAppSub?.cancel();
    _breathController.dispose();
    _tailController.dispose();
    _blinkController.dispose();
    _walkController.dispose();
    _blinkTimer?.cancel();
    _inputController.dispose();
    super.dispose();
  }

  void _handleMainAppMessage(String raw) {
    try {
      final json = jsonDecode(raw) as Map<String, dynamic>;
      final type = json['type'] as String;
      final data = (json['data'] as Map<String, dynamic>?) ?? {};

      switch (type) {
        case 'config':
          setState(() {
            _baseUrl = data['baseUrl'] as String? ?? _baseUrl;
            _apiKey = data['apiKey'] as String? ?? _apiKey;
            _sessionId = data['sessionId'] as String?;
            ApiClient.configure(baseUrl: _baseUrl, apiKey: _apiKey);
          });
          break;
        case 'close':
          FlutterOverlayWindow.closeOverlay();
          break;
      }
    } catch (e) {
      debugPrint('悬浮窗消息解析失败: $e');
    }
  }

  Future<void> _sendMessage(String text) async {
    if (text.trim().isEmpty) return;

    ApiClient.configure(baseUrl: _baseUrl, apiKey: _apiKey);

    setState(() {
      _showInput = false;
      _mood = _PetMood.thinking;
      _isThinking = true;
      _bubbleText = '思考中...';
      _replyBuffer.clear();
    });

    try {
      final result = ChatApi.sendMessageStream(
        text: text,
        sessionId: _sessionId,
      );
      _sseClient = result.client;

      _streamSub?.cancel();
      _streamSub = result.stream.listen(
        (event) {
          if (event.type == 'heartbeat') return;

          if (event.type == 'error') {
            setState(() {
              _mood = _PetMood.idle;
              _isThinking = false;
              _bubbleText = '出错了: ${event.content ?? '未知错误'}';
            });
            return;
          }

          if (event.type == 'thinking') {
            setState(() {
              _mood = _PetMood.thinking;
              _bubbleText = event.content ?? '思考中...';
            });
            return;
          }

          if (event.content != null && event.content!.isNotEmpty) {
            _replyBuffer.write(event.content);
            setState(() {
              _mood = _PetMood.talking;
              _isThinking = false;
              _bubbleText = _replyBuffer.toString();
            });
          }

          if (event.done == true) {
            if (event.sessionId != null) {
              _sessionId = event.sessionId;
            }
            setState(() => _mood = _PetMood.happy);
            Future.delayed(const Duration(seconds: 4), () {
              if (mounted) {
                setState(() {
                  _mood = _PetMood.idle;
                  _bubbleText = '';
                });
              }
            });
          }
        },
        onError: (err) {
          setState(() {
            _mood = _PetMood.idle;
            _isThinking = false;
            _bubbleText = '连接失败: $err';
          });
        },
        onDone: () {
          if (mounted && _isThinking) {
            setState(() {
              _mood = _PetMood.idle;
              _isThinking = false;
            });
          }
        },
      );
    } catch (e) {
      setState(() {
        _mood = _PetMood.idle;
        _isThinking = false;
        _bubbleText = '发送失败: $e';
      });
    }
  }

  void _onPetTap() {
    if (_showInput) {
      setState(() => _showInput = false);
    } else {
      setState(() {
        _showInput = true;
        _mood = _PetMood.happy;
      });
      Future.delayed(const Duration(seconds: 8), () {
        if (mounted && _showInput && _inputController.text.isEmpty) {
          setState(() {
            _showInput = false;
            _mood = _PetMood.idle;
          });
        }
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.transparent,
      child: GestureDetector(
        onTap: _onPetTap,
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          mainAxisSize: MainAxisSize.min,
          children: [
            // 气泡消息
            if (_bubbleText.isNotEmpty)
              Flexible(
                child: Container(
                  constraints: const BoxConstraints(maxWidth: 220),
                  margin: const EdgeInsets.only(bottom: 8),
                  padding: const EdgeInsets.symmetric(
                      horizontal: 12, vertical: 8),
                  decoration: BoxDecoration(
                    color: Colors.white.withOpacity(0.95),
                    borderRadius: BorderRadius.circular(12),
                    boxShadow: [
                      BoxShadow(
                        color: Colors.black.withOpacity(0.1),
                        blurRadius: 6,
                        offset: const Offset(0, 2),
                      ),
                    ],
                  ),
                  child: Text(
                    _bubbleText,
                    style: const TextStyle(
                      fontSize: 12,
                      color: Colors.black87,
                      height: 1.35,
                    ),
                    maxLines: 6,
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
              ),
            // Canvas 猫宠物
            AnimatedBuilder(
              animation: Listenable.merge([
                _breathController,
                _tailController,
                _blinkController,
                _walkController,
              ]),
              builder: (context, _) {
                return CustomPaint(
                  size: const Size(160, 180),
                  painter: _CatPainter(
                    mood: _mood,
                    breath: _breathController.value,
                    tail: _tailController.value,
                    blink: _blinkController.value,
                    walk: _walkController.value,
                  ),
                );
              },
            ),
            // 输入框
            if (_showInput) ...[
              const SizedBox(height: 8),
              Container(
                width: 240,
                padding: const EdgeInsets.symmetric(horizontal: 4),
                child: Row(
                  children: [
                    Expanded(
                      child: TextField(
                        controller: _inputController,
                        style: const TextStyle(fontSize: 12),
                        decoration: InputDecoration(
                          hintText: '说点什么吧~',
                          hintStyle: const TextStyle(fontSize: 11),
                          isDense: true,
                          contentPadding: const EdgeInsets.symmetric(
                              horizontal: 10, vertical: 7),
                          filled: true,
                          fillColor: Colors.white.withOpacity(0.95),
                          border: OutlineInputBorder(
                            borderRadius: BorderRadius.circular(18),
                            borderSide: BorderSide.none,
                          ),
                        ),
                        onSubmitted: (text) {
                          _sendMessage(text);
                          _inputController.clear();
                        },
                      ),
                    ),
                    const SizedBox(width: 6),
                    GestureDetector(
                      onTap: () {
                        final text = _inputController.text;
                        if (text.isNotEmpty) {
                          _sendMessage(text);
                          _inputController.clear();
                        }
                      },
                      child: Container(
                        width: 34,
                        height: 34,
                        decoration: const BoxDecoration(
                          color: Color(0xFF6366F1),
                          shape: BoxShape.circle,
                          boxShadow: [
                            BoxShadow(
                              color: Colors.black12,
                              blurRadius: 4,
                              offset: Offset(0, 2),
                            ),
                          ],
                        ),
                        child: const Icon(
                          Icons.send,
                          color: Colors.white,
                          size: 16,
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

/// Canvas 猫角色绘制器
class _CatPainter extends CustomPainter {
  final _PetMood mood;
  final double breath; // 0.0 ~ 1.0
  final double tail;   // 0.0 ~ 1.0
  final double blink;  // 0.0 ~ 1.0
  final double walk;   // 0.0 ~ 1.0

  _CatPainter({
    required this.mood,
    required this.breath,
    required this.tail,
    required this.blink,
    required this.walk,
  });

  @override
  void paint(Canvas canvas, Size size) {
    final cx = size.width / 2;
    final cy = size.height / 2 + 10;

    // 呼吸缩放
    final breathScale = 1.0 + sin(breath * pi) * 0.025;
    // 身体微浮
    final bodyY = cy - sin(breath * pi) * 2;
    // 走路时身体微左右摇
    final walkSway = sin(walk * pi * 2) * 3;

    canvas.save();
    canvas.translate(walkSway, 0);

    // === 尾巴 ===
    _drawTail(canvas, cx, cy);

    // === 身体 ===
    _drawBody(canvas, cx, bodyY, breathScale);

    // === 头 ===
    _drawHead(canvas, cx, bodyY, breathScale);

    // === 前腿 ===
    _drawLegs(canvas, cx, bodyY, breathScale);

    canvas.restore();
  }

  void _drawTail(Canvas canvas, double cx, double cy) {
    final tailBaseX = cx + 38;
    final tailBaseY = cy + 10;
    // 尾巴摆动角度
    final swing = (tail - 0.5) * 0.6; // -0.3 ~ 0.3 弧度

    final tailColor = Paint()
      ..color = const Color(0xFFE8A87C)
      ..style = PaintingStyle.fill
      ..strokeWidth = 10
      ..strokeCap = StrokeCap.round;

    // 尾巴用贝塞尔曲线画
    final path = Path();
    path.moveTo(tailBaseX, tailBaseY);
    // 控制点随 swing 变化
    final cp1x = tailBaseX + 15 + swing * 10;
    final cp1y = tailBaseY - 20;
    final cp2x = tailBaseX + 25 + swing * 20;
    final cp2y = tailBaseY - 40;
    final endX = tailBaseX + 20 + swing * 30;
    final endY = tailBaseY - 55;
    path.cubicTo(cp1x, cp1y, cp2x, cp2y, endX, endY);

    // 画粗线作为尾巴
    canvas.drawPath(path, tailColor);

    // 尾巴尖的毛球
    canvas.drawCircle(
      Offset(endX, endY),
      7,
      Paint()..color = const Color(0xFFF5C99B),
    );
  }

  void _drawBody(Canvas canvas, double cx, double cy, double scale) {
    final bodyW = 55 * scale;
    final bodyH = 45 * scale;
    final bodyY = cy + 20;

    // 身体（椭圆形）
    final bodyRect = Rect.fromCenter(
      center: Offset(cx, bodyY),
      width: bodyW,
      height: bodyH,
    );
    final bodyPaint = Paint()
      ..shader = LinearGradient(
        colors: [const Color(0xFFF5C99B), const Color(0xFFE8A87C)],
        begin: Alignment.topCenter,
        end: Alignment.bottomCenter,
      ).createShader(bodyRect);
    canvas.drawOval(bodyRect, bodyPaint);

    // 肚子（浅色椭圆）
    canvas.drawOval(
      Rect.fromCenter(
        center: Offset(cx, bodyY + 5),
        width: bodyW * 0.6,
        height: bodyH * 0.7,
      ),
      Paint()..color = const Color(0xFFFEF5E7),
    );
  }

  void _drawHead(Canvas canvas, double cx, double cy, double scale) {
    final headR = 38 * scale;
    final headY = cy - 15;

    // === 猫耳 ===
    _drawEars(canvas, cx, headY, headR);

    // === 脸 ===
    final facePaint = Paint()
      ..shader = RadialGradient(
        colors: [const Color(0xFFF8D5A0), const Color(0xFFE8A87C)],
        center: const Alignment(-0.2, -0.3),
        radius: 0.9,
      ).createShader(Rect.fromCircle(
        center: Offset(cx, headY),
        radius: headR,
      ));
    canvas.drawCircle(Offset(cx, headY), headR, facePaint);

    // === 脸颊白色区域 ===
    canvas.drawOval(
      Rect.fromCenter(
        center: Offset(cx, headY + 8),
        width: headR * 1.2,
        height: headR * 0.8,
      ),
      Paint()..color = const Color(0xFFFEF5E7),
    );

    // === 眼睛 ===
    _drawEyes(canvas, cx, headY, headR);

    // === 鼻子 ===
    _drawNose(canvas, cx, headY, headR);

    // === 嘴巴 ===
    _drawMouth(canvas, cx, headY, headR);

    // === 猫须 ===
    _drawWhiskers(canvas, cx, headY, headR);

    // === 腮红 ===
    if (mood == _PetMood.happy || mood == _PetMood.talking) {
      final blushPaint = Paint()
        ..color = const Color(0xFFFF8A80).withOpacity(0.4);
      canvas.drawCircle(
        Offset(cx - headR * 0.5, headY + headR * 0.25),
        headR * 0.13,
        blushPaint,
      );
      canvas.drawCircle(
        Offset(cx + headR * 0.5, headY + headR * 0.25),
        headR * 0.13,
        blushPaint,
      );
    }
  }

  void _drawEars(Canvas canvas, double cx, double headY, double headR) {
    final earPaint = Paint()
      ..color = const Color(0xFFE8A87C)
      ..style = PaintingStyle.fill;

    final innerEarPaint = Paint()
      ..color = const Color(0xFFFFCDD2)
      ..style = PaintingStyle.fill;

    // 左耳
    final leftEar = Path()
      ..moveTo(cx - headR * 0.7, headY - headR * 0.3)
      ..lineTo(cx - headR * 0.9, headY - headR * 1.1)
      ..lineTo(cx - headR * 0.25, headY - headR * 0.65)
      ..close();
    canvas.drawPath(leftEar, earPaint);
    // 左耳内侧
    final leftInner = Path()
      ..moveTo(cx - headR * 0.62, headY - headR * 0.45)
      ..lineTo(cx - headR * 0.72, headY - headR * 0.85)
      ..lineTo(cx - headR * 0.42, headY - headR * 0.6)
      ..close();
    canvas.drawPath(leftInner, innerEarPaint);

    // 右耳
    final rightEar = Path()
      ..moveTo(cx + headR * 0.7, headY - headR * 0.3)
      ..lineTo(cx + headR * 0.9, headY - headR * 1.1)
      ..lineTo(cx + headR * 0.25, headY - headR * 0.65)
      ..close();
    canvas.drawPath(rightEar, earPaint);
    // 右耳内侧
    final rightInner = Path()
      ..moveTo(cx + headR * 0.62, headY - headR * 0.45)
      ..lineTo(cx + headR * 0.72, headY - headR * 0.85)
      ..lineTo(cx + headR * 0.42, headY - headR * 0.6)
      ..close();
    canvas.drawPath(rightInner, innerEarPaint);
  }

  void _drawEyes(Canvas canvas, double cx, double headY, double headR) {
    final eyeOffsetX = headR * 0.35;
    final eyeY = headY - headR * 0.05;
    final eyeW = headR * 0.22;
    // 眨眼时高度缩小
    final eyeH = headR * 0.28 * (1.0 - blink);

    if (eyeH < 2) {
      // 闭眼 — 画弧线
      final linePaint = Paint()
        ..color = const Color(0xFF3E2723)
        ..strokeWidth = 2.5
        ..strokeCap = StrokeCap.round
        ..style = PaintingStyle.stroke;
      canvas.drawArc(
        Rect.fromCenter(
          center: Offset(cx - eyeOffsetX, eyeY),
          width: eyeW * 1.2,
          height: 8,
        ),
        0, pi, false, linePaint,
      );
      canvas.drawArc(
        Rect.fromCenter(
          center: Offset(cx + eyeOffsetX, eyeY),
          width: eyeW * 1.2,
          height: 8,
        ),
        0, pi, false, linePaint,
      );
      return;
    }

    // 眼白
    final eyePaint = Paint()
      ..color = const Color(0xFF3E2723)
      ..style = PaintingStyle.fill;

    canvas.drawOval(
      Rect.fromCenter(
        center: Offset(cx - eyeOffsetX, eyeY),
        width: eyeW,
        height: eyeH,
      ),
      eyePaint,
    );
    canvas.drawOval(
      Rect.fromCenter(
        center: Offset(cx + eyeOffsetX, eyeY),
        width: eyeW,
        height: eyeH,
      ),
      eyePaint,
    );

    // 瞳孔高光
    if (eyeH > 5) {
      final hlPaint = Paint()..color = Colors.white;
      canvas.drawCircle(
        Offset(cx - eyeOffsetX + eyeW * 0.15, eyeY - eyeH * 0.25),
        eyeW * 0.15,
        hlPaint,
      );
      canvas.drawCircle(
        Offset(cx + eyeOffsetX + eyeW * 0.15, eyeY - eyeH * 0.25),
        eyeW * 0.15,
        hlPaint,
      );
      // 小高光
      canvas.drawCircle(
        Offset(cx - eyeOffsetX - eyeW * 0.1, eyeY + eyeH * 0.15),
        eyeW * 0.08,
        hlPaint,
      );
      canvas.drawCircle(
        Offset(cx + eyeOffsetX - eyeW * 0.1, eyeY + eyeH * 0.15),
        eyeW * 0.08,
        hlPaint,
      );
    }
  }

  void _drawNose(Canvas canvas, double cx, double headY, double headR) {
    final noseY = headY + headR * 0.15;
    // 粉色倒三角鼻子
    final nosePath = Path()
      ..moveTo(cx - 4, noseY)
      ..lineTo(cx + 4, noseY)
      ..lineTo(cx, noseY + 4)
      ..close();
    canvas.drawPath(
      nosePath,
      Paint()..color = const Color(0xFFEF9A9A),
    );
  }

  void _drawMouth(Canvas canvas, double cx, double headY, double headR) {
    final mouthY = headY + headR * 0.3;
    final paint = Paint()
      ..color = const Color(0xFF3E2723)
      ..strokeWidth = 2
      ..strokeCap = StrokeCap.round
      ..style = PaintingStyle.stroke;

    switch (mood) {
      case _PetMood.talking:
        // 张嘴说话
        final mouthH = 6.0 + sin(walk * pi * 4) * 3;
        canvas.drawOval(
          Rect.fromCenter(
            center: Offset(cx, mouthY),
            width: 12,
            height: mouthH,
          ),
          Paint()..color = const Color(0xFFE53935),
        );
        break;
      case _PetMood.happy:
        // 开心大笑
        canvas.drawArc(
          Rect.fromCenter(
            center: Offset(cx, mouthY - 3),
            width: 18,
            height: 14,
          ),
          0, pi, false, paint,
        );
        // 牙齿
        canvas.drawRect(
          Rect.fromCenter(
            center: Offset(cx, mouthY),
            width: 6,
            height: 4,
          ),
          Paint()..color = Colors.white,
        );
        break;
      case _PetMood.thinking:
        // 思考 — 歪嘴
        canvas.drawLine(
          Offset(cx - 5, mouthY),
          Offset(cx + 5, mouthY - 3),
          paint,
        );
        break;
      case _PetMood.idle:
        // 默认微笑 — 两条弧线组成 ω 形
        canvas.drawArc(
          Rect.fromCenter(
            center: Offset(cx - 4, mouthY),
            width: 8,
            height: 6,
          ),
          0, pi, false, paint,
        );
        canvas.drawArc(
          Rect.fromCenter(
            center: Offset(cx + 4, mouthY),
            width: 8,
            height: 6,
          ),
          0, pi, false, paint,
        );
        break;
    }
  }

  void _drawWhiskers(Canvas canvas, double cx, double headY, double headR) {
    final whiskerY = headY + headR * 0.2;
    final paint = Paint()
      ..color = const Color(0xFF8D6E63)
      ..strokeWidth = 1.2
      ..strokeCap = StrokeCap.round;

    // 左侧 3 根
    canvas.drawLine(
      Offset(cx - headR * 0.3, whiskerY),
      Offset(cx - headR * 0.9, whiskerY - 3),
      paint,
    );
    canvas.drawLine(
      Offset(cx - headR * 0.3, whiskerY + 2),
      Offset(cx - headR * 0.9, whiskerY + 2),
      paint,
    );
    canvas.drawLine(
      Offset(cx - headR * 0.3, whiskerY + 4),
      Offset(cx - headR * 0.85, whiskerY + 8),
      paint,
    );

    // 右侧 3 根
    canvas.drawLine(
      Offset(cx + headR * 0.3, whiskerY),
      Offset(cx + headR * 0.9, whiskerY - 3),
      paint,
    );
    canvas.drawLine(
      Offset(cx + headR * 0.3, whiskerY + 2),
      Offset(cx + headR * 0.9, whiskerY + 2),
      paint,
    );
    canvas.drawLine(
      Offset(cx + headR * 0.3, whiskerY + 4),
      Offset(cx + headR * 0.85, whiskerY + 8),
      paint,
    );
  }

  void _drawLegs(Canvas canvas, double cx, double cy, double scale) {
    final legPaint = Paint()
      ..color = const Color(0xFFE8A87C)
      ..style = PaintingStyle.fill;

    final legY = cy + 38;
    final legW = 12.0;
    final legH = 14.0;

    // 走路时腿上下交替
    final leftLegY = legY + sin(walk * pi * 2) * 3;
    final rightLegY = legY - sin(walk * pi * 2) * 3;

    // 左前腿
    canvas.drawRRect(
      RRect.fromRectAndRadius(
        Rect.fromCenter(
          center: Offset(cx - 12, leftLegY),
          width: legW,
          height: legH,
        ),
        const Radius.circular(6),
      ),
      legPaint,
    );
    // 右前腿
    canvas.drawRRect(
      RRect.fromRectAndRadius(
        Rect.fromCenter(
          center: Offset(cx + 12, rightLegY),
          width: legW,
          height: legH,
        ),
        const Radius.circular(6),
      ),
      legPaint,
    );

    // 爪子白色
    final pawPaint = Paint()..color = const Color(0xFFFEF5E7);
    canvas.drawOval(
      Rect.fromCenter(
        center: Offset(cx - 12, leftLegY + legH * 0.3),
        width: legW * 1.1,
        height: 6,
      ),
      pawPaint,
    );
    canvas.drawOval(
      Rect.fromCenter(
        center: Offset(cx + 12, rightLegY + legH * 0.3),
        width: legW * 1.1,
        height: 6,
      ),
      pawPaint,
    );
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => true;
}
