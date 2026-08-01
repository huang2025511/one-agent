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
/// main.dart 通过 `import 'screens/overlay_pet_screen.dart'` 引用本文件，
/// 因此本文件中的 OverlayPetScreen 类不会被 tree-shaking 移除。
///
/// 历史教训：曾经在 main.dart 和本文件各定义一个 overlayMain()，
/// 导致 Dart 顶层函数同名冲突，Flutter 引擎无法定位悬浮窗入口点，
/// 悬浮窗完全无法启动（权限已开也无效）。

/// 悬浮窗内的桌宠页面
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
  String? _modelDir; // 模型目录（assets 或文件系统路径）
  String? _modelFileName; // 模型文件名

  // 宠物状态
  PetMood _mood = PetMood.idle;
  double _mouthOpen = 0.0;

  // 聊天状态
  String _bubbleText = ''; // 气泡文字
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
            _modelDir = data['modelPath'] as String?;
            _modelFileName = data['modelFileName'] as String?;
            // 配置 ApiClient（悬浮窗内独立配置）
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

    // 确保 ApiClient 已配置
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
          // 心跳忽略
          if (event.type == 'heartbeat') return;

          // 错误
          if (event.type == 'error') {
            setState(() {
              _mood = PetMood.idle;
              _isThinking = false;
              _bubbleText = '出错了: ${event.content ?? '未知错误'}';
            });
            return;
          }

          // 思考事件
          if (event.type == 'thinking') {
            setState(() {
              _mood = PetMood.thinking;
              _bubbleText = event.content ?? '思考中...';
            });
            return;
          }

          // 内容事件 → 切换到说话模式
          if (event.content != null && event.content!.isNotEmpty) {
            _replyBuffer.write(event.content);
            setState(() {
              _mood = PetMood.talking;
              _isThinking = false;
              _mouthOpen = 0.5 + (event.content!.length % 5) * 0.1;
              _bubbleText = _replyBuffer.toString();
            });
          }

          // 完成
          if (event.done == true) {
            if (event.sessionId != null) {
              _sessionId = event.sessionId;
            }
            setState(() {
              _mood = PetMood.happy;
              _mouthOpen = 0.0;
            });
            // 3 秒后回到 idle
            Future.delayed(const Duration(seconds: 3), () {
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

  /// 点击宠物
  void _onPetTap() {
    if (_showInput) {
      setState(() => _showInput = false);
    } else {
      setState(() {
        _showInput = true;
        _mood = PetMood.happy;
      });
      // 3 秒不输入自动收起
      Future.delayed(const Duration(seconds: 5), () {
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
            children: [
              // 气泡
              if (_bubbleText.isNotEmpty)
                Flexible(
                  child: Container(
                    constraints: const BoxConstraints(maxWidth: 180),
                    margin: const EdgeInsets.only(bottom: 4),
                    padding: const EdgeInsets.symmetric(
                        horizontal: 10, vertical: 6),
                    decoration: BoxDecoration(
                      color: Colors.white.withOpacity(0.95),
                      borderRadius: BorderRadius.circular(12),
                      boxShadow: [
                        BoxShadow(
                          color: Colors.black.withOpacity(0.1),
                          blurRadius: 4,
                          offset: const Offset(0, 2),
                        ),
                      ],
                    ),
                    child: Text(
                      _bubbleText,
                      style: const TextStyle(
                        fontSize: 11,
                        color: Colors.black87,
                        height: 1.3,
                      ),
                      maxLines: 4,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
                ),
              // 宠物（模型路径变化时通过 key 重建以重新加载）
              PetRenderer(
                key: ValueKey('${_modelDir ?? ''}/${_modelFileName ?? ''}'),
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
                const SizedBox(height: 4),
                Container(
                  width: 160,
                  padding: const EdgeInsets.symmetric(horizontal: 4),
                  child: Row(
                    children: [
                      Expanded(
                        child: TextField(
                          controller: _inputController,
                          style: const TextStyle(fontSize: 12),
                          decoration: InputDecoration(
                            hintText: '说点什么...',
                            hintStyle: const TextStyle(fontSize: 11),
                            isDense: true,
                            contentPadding: const EdgeInsets.symmetric(
                                horizontal: 8, vertical: 6),
                            filled: true,
                            fillColor: Colors.white.withOpacity(0.95),
                            border: OutlineInputBorder(
                              borderRadius: BorderRadius.circular(16),
                              borderSide: BorderSide.none,
                            ),
                          ),
                          onSubmitted: (text) {
                            _sendMessage(text);
                            _inputController.clear();
                          },
                        ),
                      ),
                      const SizedBox(width: 4),
                      GestureDetector(
                        onTap: () {
                          final text = _inputController.text;
                          if (text.isNotEmpty) {
                            _sendMessage(text);
                            _inputController.clear();
                          }
                        },
                        child: Container(
                          width: 28,
                          height: 28,
                          decoration: const BoxDecoration(
                            color: Color(0xFF6366F1),
                            shape: BoxShape.circle,
                          ),
                          child: const Icon(
                            Icons.send,
                            color: Colors.white,
                            size: 14,
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
