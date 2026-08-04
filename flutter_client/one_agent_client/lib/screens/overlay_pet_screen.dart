import 'dart:async';
import 'dart:convert';
import 'dart:math';
import 'package:flutter/material.dart';
import 'package:flutter_overlay_window/flutter_overlay_window.dart';

import '../api/api_client.dart';
import '../api/chat_api.dart';
import '../api/sse_client.dart';

/// ⚠️ 悬浮窗入口 overlayMain() 已在 main.dart 中统一定义（避免重复符号冲突）
/// main.dart 通过 `import 'screens/overlay_pet_screen.dart'` 引用本文件，
/// 因此本文件中的 OverlayPetScreen 类不会被 tree-shaking 移除。

/// 悬浮窗内的桌宠页面 - 使用 Canvas 绘制，带动画+气泡+聊天
class OverlayPetScreen extends StatefulWidget {
  const OverlayPetScreen({super.key});

  @override
  State<OverlayPetScreen> createState() => _OverlayPetScreenState();
}

enum _PetMood { idle, thinking, talking, happy }

class _OverlayPetScreenState extends State<OverlayPetScreen>
    with SingleTickerProviderStateMixin {
  // 配置（由主 APP 通过 flutter_overlay_window 传入）
  String _baseUrl = 'http://127.0.0.1:18792';
  String _apiKey = '';
  String? _sessionId;

  // 宠物状态
  _PetMood _mood = _PetMood.idle;
  late final AnimationController _animController;
  late final Animation<double> _breathAnimation;
  late final Animation<double> _blinkAnimation;
  bool _isBlinking = false;
  Timer? _blinkTimer;

  // 聊天状态
  String _bubbleText = '';
  bool _isThinking = false;
  final StringBuffer _replyBuffer = StringBuffer();

  // SSE
  StreamSubscription<StreamEvent>? _streamSub;
  SseClient? _sseClient;

  // 输入框（默认不显示，点击宠物弹出）
  final TextEditingController _inputController = TextEditingController();
  bool _showInput = false;

  // 主 APP 消息监听
  StreamSubscription? _mainAppSub;

  @override
  void initState() {
    super.initState();

    // 初始化动画控制器
    _animController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 2000),
    )..repeat(reverse: true);

    _breathAnimation = Tween<double>(begin: 1.0, end: 1.05).animate(
      CurvedAnimation(parent: _animController, curve: Curves.easeInOut),
    );

    _blinkAnimation = Tween<double>(begin: 1.0, end: 0.1).animate(
      CurvedAnimation(
        parent: _animController,
        curve: const Interval(0.45, 0.55, curve: Curves.easeInOut),
      ),
    );

    // 定时眨眼
    _scheduleBlink();

    // 监听主 APP 发来的消息
    _mainAppSub = FlutterOverlayWindow.overlayListener.listen((event) {
      _handleMainAppMessage(event.toString());
    });
  }

  void _scheduleBlink() {
    _blinkTimer?.cancel();
    final delay = Duration(seconds: 3 + Random().nextInt(5));
    _blinkTimer = Timer(delay, () {
      if (mounted) {
        setState(() => _isBlinking = true);
        Future.delayed(const Duration(milliseconds: 150), () {
          if (mounted) {
            setState(() => _isBlinking = false);
            _scheduleBlink();
          }
        });
      }
    });
  }

  @override
  void dispose() {
    _streamSub?.cancel();
    _sseClient?.dispose();
    _mainAppSub?.cancel();
    _animController.dispose();
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

  /// 发送消息并流式接收回复
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

  /// 点击宠物 → 显示/隐藏输入框
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

  /// 绘制宠物
  Widget _buildPet() {
    return AnimatedBuilder(
      animation: _animController,
      builder: (context, child) {
        return CustomPaint(
          size: const Size(140, 140),
          painter: _PetPainter(
            mood: _mood,
            breathScale: _breathAnimation.value,
            eyeOpen: _isBlinking ? _blinkAnimation.value : 1.0,
            mouthOpen: _mood == _PetMood.talking ? 0.3 : 0.0,
          ),
        );
      },
    );
  }

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Colors.transparent,
      child: GestureDetector(
        onTap: _onPetTap,
        child: Container(
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(16),
          ),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            mainAxisSize: MainAxisSize.min,
            children: [
              // 气泡消息
              if (_bubbleText.isNotEmpty)
                Flexible(
                  child: Container(
                    constraints: const BoxConstraints(maxWidth: 220),
                    margin: const EdgeInsets.only(bottom: 10),
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
              // Canvas 宠物
              _buildPet(),
              // 输入框
              if (_showInput) ...[
                const SizedBox(height: 10),
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
      ),
    );
  }
}

/// Canvas 宠物绘制器（一只可爱的紫色圆形小怪兽）
class _PetPainter extends CustomPainter {
  final _PetMood mood;
  final double breathScale;
  final double eyeOpen;
  final double mouthOpen;

  _PetPainter({
    required this.mood,
    required this.breathScale,
    required this.eyeOpen,
    required this.mouthOpen,
  });

  @override
  void paint(Canvas canvas, Size size) {
    final center = Offset(size.width / 2, size.height / 2);
    final radius = size.width / 2 * breathScale;

    // 身体
    final bodyGrad = LinearGradient(
      colors: [
        const Color(0xFF818CF8),
        const Color(0xFF6366F1),
        const Color(0xFF4F46E5),
      ],
      begin: Alignment.topLeft,
      end: Alignment.bottomRight,
    );
    final bodyPaint = Paint()
      ..shader = bodyGrad.createShader(
        Rect.fromCircle(center: center, radius: radius),
      )
      ..style = PaintingStyle.fill;
    canvas.drawCircle(center, radius, bodyPaint);

    // 耳朵（两个小三角）
    final earPaint = Paint()
      ..color = const Color(0xFF8B5CF6)
      ..style = PaintingStyle.fill;
    final leftEarPath = Path()
      ..moveTo(center.dx - radius * 0.7, center.dy - radius * 0.4)
      ..lineTo(center.dx - radius * 0.95, center.dy - radius * 1.05)
      ..lineTo(center.dx - radius * 0.3, center.dy - radius * 0.75)
      ..close();
    canvas.drawPath(leftEarPath, earPaint);
    final rightEarPath = Path()
      ..moveTo(center.dx + radius * 0.7, center.dy - radius * 0.4)
      ..lineTo(center.dx + radius * 0.95, center.dy - radius * 1.05)
      ..lineTo(center.dx + radius * 0.3, center.dy - radius * 0.75)
      ..close();
    canvas.drawPath(rightEarPath, earPaint);

    // 小触角（头顶）
    final antennaPaint = Paint()
      ..color = const Color(0xFF4F46E5)
      ..strokeWidth = 2.5
      ..strokeCap = StrokeCap.round
      ..style = PaintingStyle.stroke;
    canvas.drawLine(
      Offset(center.dx, center.dy - radius * 0.95),
      Offset(center.dx - radius * 0.08, center.dy - radius * 1.25),
      antennaPaint,
    );
    canvas.drawCircle(
      Offset(center.dx - radius * 0.08, center.dy - radius * 1.28),
      4,
      Paint()..color = const Color(0xFFF472B6),
    );

    // 身体边框高光
    final borderPaint = Paint()
      ..color = Colors.white.withOpacity(0.2)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2;
    canvas.drawArc(
      Rect.fromCircle(center: center, radius: radius - 1),
      -pi * 0.8,
      pi * 0.6,
      false,
      borderPaint,
    );

    // 眼睛
    final eyeOffsetX = radius * 0.32;
    final eyeRadius = radius * 0.14;
    final eyeY = center.dy - radius * 0.05;

    final eyeBgPaint = Paint()
      ..color = Colors.white
      ..style = PaintingStyle.fill;

    canvas.drawOval(
      Rect.fromCenter(
        center: Offset(center.dx - eyeOffsetX, eyeY),
        width: eyeRadius * 1.8,
        height: eyeRadius * 2 * eyeOpen,
      ),
      eyeBgPaint,
    );
    canvas.drawOval(
      Rect.fromCenter(
        center: Offset(center.dx + eyeOffsetX, eyeY),
        width: eyeRadius * 1.8,
        height: eyeRadius * 2 * eyeOpen,
      ),
      eyeBgPaint,
    );

    // 瞳孔
    if (eyeOpen > 0.3) {
      final pupilPaint = Paint()
        ..color = const Color(0xFF1E1B4B)
        ..style = PaintingStyle.fill;
      final pr = eyeRadius * 0.6 * eyeOpen;

      canvas.drawCircle(
        Offset(center.dx - eyeOffsetX + pr * 0.2, eyeY + pr * 0.1),
        pr,
        pupilPaint,
      );
      canvas.drawCircle(
        Offset(center.dx + eyeOffsetX + pr * 0.2, eyeY + pr * 0.1),
        pr,
        pupilPaint,
      );

      // 高光
      final hlPaint = Paint()
        ..color = Colors.white
        ..style = PaintingStyle.fill;
      canvas.drawCircle(
        Offset(center.dx - eyeOffsetX - pr * 0.2, eyeY - pr * 0.3),
        pr * 0.25,
        hlPaint,
      );
      canvas.drawCircle(
        Offset(center.dx + eyeOffsetX - pr * 0.2, eyeY - pr * 0.3),
        pr * 0.25,
        hlPaint,
      );
    }

    // 嘴巴
    final mouthY = center.dy + radius * 0.25;
    final mouthPaint = Paint()
      ..color = const Color(0xFF312E81)
      ..style = PaintingStyle.fill;
    final tonguePaint = Paint()
      ..color = const Color(0xFFFB7185)
      ..style = PaintingStyle.fill;

    if (mood == _PetMood.thinking) {
      // 思考时画一个O型嘴
      canvas.drawCircle(
        Offset(center.dx, mouthY),
        radius * 0.08,
        mouthPaint,
      );
    } else if (mood == _PetMood.talking) {
      // 说话时嘴巴张开
      final mh = radius * 0.15 + mouthOpen * radius * 0.2;
      final mouthRect = Rect.fromCenter(
        center: Offset(center.dx, mouthY),
        width: radius * 0.35,
        height: mh,
      );
      canvas.drawOval(mouthRect, mouthPaint);
      // 小舌头
      canvas.drawCircle(
        Offset(center.dx, mouthY + mh * 0.15),
        radius * 0.05,
        tonguePaint,
      );
    } else {
      // idle/happy: 微笑
      final smilePath = Path()
        ..moveTo(center.dx - radius * 0.18, mouthY)
        ..quadraticBezierTo(
          center.dx,
          mouthY + radius * (mood == _PetMood.happy ? 0.18 : 0.1),
          center.dx + radius * 0.18,
          mouthY,
        );
      final smilePaint = Paint()
        ..color = const Color(0xFF312E81)
        ..strokeWidth = 2.5
        ..strokeCap = StrokeCap.round
        ..style = PaintingStyle.stroke;
      canvas.drawPath(smilePath, smilePaint);

      if (mood == _PetMood.happy) {
        // 开心时露出小牙齿
        final toothPaint = Paint()
          ..color = Colors.white
          ..style = PaintingStyle.fill;
        canvas.drawRect(
          Rect.fromCenter(
            center: Offset(center.dx, mouthY + radius * 0.03),
            width: radius * 0.08,
            height: radius * 0.06,
          ),
          toothPaint,
        );
      }
    }

    // 腮红（开心时）
    if (mood == _PetMood.happy || mood == _PetMood.talking) {
      final blushPaint = Paint()
        ..color = const Color(0xFFFF6B9D).withOpacity(0.35)
        ..style = PaintingStyle.fill;
      canvas.drawCircle(
        Offset(center.dx - radius * 0.55, eyeY + radius * 0.35),
        radius * 0.13,
        blushPaint,
      );
      canvas.drawCircle(
        Offset(center.dx + radius * 0.55, eyeY + radius * 0.35),
        radius * 0.13,
        blushPaint,
      );
    }
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => true;
}
