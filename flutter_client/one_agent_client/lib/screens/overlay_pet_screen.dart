import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_overlay_window/flutter_overlay_window.dart';

import '../api/api_client.dart';
import '../api/chat_api.dart';
import '../api/sse_client.dart';
import '../models/chat_message.dart';
import '../services/pet_renderer.dart';

/// ⚠️ 悬浮窗入口 overlayMain() 已在 main.dart 中统一定义（避免重复符号冲突）

/// 悬浮窗内的桌宠页面
///
/// 使用 PetRenderer 渲染宠物：
/// - 优先尝试 Live2D 模型（PlatformView）
/// - 失败时自动 fallback 到 Canvas 绘制
///
/// 同时提供气泡消息显示和聊天输入功能。
class OverlayPetScreen extends StatefulWidget {
  const OverlayPetScreen({super.key});

  @override
  State<OverlayPetScreen> createState() => _OverlayPetScreenState();
}

class _OverlayPetScreenState extends State<OverlayPetScreen> {
  // 配置（由主 APP 通过 flutter_overlay_window 传入）
  String _baseUrl = 'http://127.0.0.1:18792';
  String _apiKey = '';
  String? _sessionId;

  // Live2D 模型路径（默认内置 mao 猫模型）
  String? _modelDir = 'assets/models/mao/';
  String? _modelFileName = 'mao_pro.model3.json';

  // 宠物状态
  PetMood _mood = PetMood.idle;
  double _mouthOpen = 0.0;

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

  // 用于强制刷新 PetRenderer（模型路径变化时）
  Key _petKey = const ValueKey('pet');

  @override
  void initState() {
    super.initState();

    // 监听主 APP 发来的消息
    _mainAppSub = FlutterOverlayWindow.overlayListener.listen((event) {
      _handleMainAppMessage(event.toString());
    });
  }

  @override
  void dispose() {
    _streamSub?.cancel();
    _sseClient?.dispose();
    _mainAppSub?.cancel();
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

            // 模型路径：仅在主 APP 显式传入时覆盖默认内置模型
            final path = data['modelPath'] as String?;
            final file = data['modelFileName'] as String?;
            if (path != null && path.isNotEmpty) _modelDir = path;
            if (file != null && file.isNotEmpty) _modelFileName = file;

            // 模型路径变化时重建 PetRenderer
            _petKey = ValueKey('${_modelDir ?? ''}/${_modelFileName ?? ''}');

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
      _mood = PetMood.thinking;
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
              _mood = PetMood.idle;
              _isThinking = false;
              _bubbleText = '出错了: ${event.content ?? '未知错误'}';
            });
            return;
          }

          if (event.type == 'thinking') {
            setState(() {
              _mood = PetMood.thinking;
              _bubbleText = event.content ?? '思考中...';
            });
            return;
          }

          if (event.content != null && event.content!.isNotEmpty) {
            _replyBuffer.write(event.content);
            setState(() {
              _mood = PetMood.talking;
              _isThinking = false;
              _mouthOpen = 0.5 + (event.content!.length % 5) * 0.1;
              _bubbleText = _replyBuffer.toString();
            });
          }

          if (event.done == true) {
            if (event.sessionId != null) {
              _sessionId = event.sessionId;
            }
            setState(() {
              _mood = PetMood.happy;
              _mouthOpen = 0.0;
            });
            Future.delayed(const Duration(seconds: 4), () {
              if (mounted) {
                setState(() {
                  _mood = PetMood.idle;
                  _bubbleText = '';
                });
              }
            });
          }
        },
        onError: (err) {
          setState(() {
            _mood = PetMood.idle;
            _isThinking = false;
            _bubbleText = '连接失败: $err';
          });
        },
        onDone: () {
          if (mounted && _isThinking) {
            setState(() {
              _mood = PetMood.idle;
              _isThinking = false;
            });
          }
        },
      );
    } catch (e) {
      setState(() {
        _mood = PetMood.idle;
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
        _mood = PetMood.happy;
      });
      Future.delayed(const Duration(seconds: 8), () {
        if (mounted && _showInput && _inputController.text.isEmpty) {
          setState(() {
            _showInput = false;
            _mood = PetMood.idle;
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
              // 宠物（PetRenderer：优先 Live2D，失败 fallback Canvas）
              PetRenderer(
                key: _petKey,
                state: PetRenderState(
                  mood: _mood,
                  mouthOpen: _mouthOpen,
                ),
                size: 160,
                modelDir: _modelDir,
                modelFileName: _modelFileName,
                onTap: _onPetTap,
              ),
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
