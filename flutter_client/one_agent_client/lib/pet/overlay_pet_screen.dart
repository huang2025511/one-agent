import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_overlay_window/flutter_overlay_window.dart';

import '../api/api_client.dart';
import '../api/chat_api.dart';
import '../api/sse_client.dart';
import 'pet_brain.dart';
import 'pet_widget.dart';

/// 悬浮窗宠物页面
///
/// 集成 Rive 动画 + PetBrain 自主行动 + SSE 对话
/// 退出时调用 [PetBrain.stop] 清理定时器
class OverlayPetScreen extends StatefulWidget {
  const OverlayPetScreen({super.key});

  @override
  State<OverlayPetScreen> createState() => _OverlayPetScreenState();
}

class _OverlayPetScreenState extends State<OverlayPetScreen> {
  // 配置
  String _baseUrl = 'http://127.0.0.1:18792';
  String _apiKey = '';
  String? _sessionId;

  // 宠物大脑
  late final PetBrain _brain;

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

    _brain = PetBrain();
    _brain.onStateChanged = (mood, action) {
      if (mounted) setState(() {});
    };
    _brain.start();

    // 监听主 APP 消息
    _mainAppSub = FlutterOverlayWindow.overlayListener.listen((event) {
      _handleMainAppMessage(event.toString());
    });
  }

  @override
  void dispose() {
    _brain.stop();
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
      _bubbleText = '';
      _isThinking = true;
      _replyBuffer.clear();
    });
    _brain.startThinking();

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
              _isThinking = false;
              _bubbleText = '出错了: ${event.content ?? '未知错误'}';
            });
            _brain.stopTalking();
            return;
          }

          if (event.type == 'thinking') {
            setState(() {
              _bubbleText = event.content ?? '思考中...';
            });
            return;
          }

          if (event.content != null && event.content!.isNotEmpty) {
            _replyBuffer.write(event.content);
            if (!_brain.mood.toString().contains('talking')) {
              _brain.startTalking();
            }
            setState(() {
              _isThinking = false;
              _bubbleText = _replyBuffer.toString();
            });
          }

          if (event.done == true) {
            if (event.sessionId != null) {
              _sessionId = event.sessionId;
            }
            _brain.stopTalking();
            // 4 秒后清除气泡
            Timer(const Duration(seconds: 4), () {
              if (mounted) {
                setState(() => _bubbleText = '');
              }
            });
          }
        },
        onError: (err) {
          setState(() {
            _isThinking = false;
            _bubbleText = '连接失败: $err';
          });
          _brain.stopTalking();
        },
        onDone: () {
          if (mounted && _isThinking) {
            setState(() => _isThinking = false);
            _brain.stopTalking();
          }
        },
      );
    } catch (e) {
      setState(() {
        _isThinking = false;
        _bubbleText = '发送失败: $e';
      });
      _brain.stopTalking();
    }
  }

  void _onPetTap() {
    _brain.onPetTap();
    setState(() {
      _showInput = !_showInput;
      if (_showInput && _bubbleText.isEmpty) {
        _bubbleText = _getRandomGreeting();
        Timer(const Duration(seconds: 3), () {
          if (mounted && _bubbleText == _getRandomGreeting()) {
            // 如果还是问候语，清掉
          }
        });
      }
    });
  }

  String _getRandomGreeting() {
    final greetings = ['喵~', '在呢~', '怎么了？', '找我吗？', '嗨！'];
    return greetings[DateTime.now().millisecond % greetings.length];
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
            // Rive 宠物
            PetWidget(
              mood: _brain.mood,
              action: _brain.currentAction,
              size: 140,
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
