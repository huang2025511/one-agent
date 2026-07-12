import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_markdown/flutter_markdown.dart';

import '../l10n/app_localizations.dart';
import '../providers/chat_provider.dart';
import '../providers/settings_provider.dart';
import '../models/chat_message.dart';
import 'session_list_screen.dart';
import 'settings_screen.dart';

/// 聊天主页面
class ChatScreen extends ConsumerStatefulWidget {
  const ChatScreen({super.key});

  @override
  ConsumerState<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends ConsumerState<ChatScreen> {
  final ScrollController _scrollController = ScrollController();
  bool _isNearBottom = true;

  @override
  void initState() {
    super.initState();
    _scrollController.addListener(() {
      if (!_scrollController.hasClients) return;
      final max = _scrollController.position.maxScrollExtent;
      final near = _scrollController.position.pixels >= max - 120;
      if (near != _isNearBottom) {
        setState(() => _isNearBottom = near);
      }
    });
  }

  @override
  void dispose() {
    _scrollController.dispose();
    super.dispose();
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scrollController.hasClients) {
        _scrollController.animateTo(
          _scrollController.position.maxScrollExtent,
          duration: const Duration(milliseconds: 200),
          curve: Curves.easeOut,
        );
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final chatState = ref.watch(chatProvider);
    final settingsState = ref.watch(settingsProvider);
    final isConnected = settingsState.isConnected;
    final l10n = AppLocalizations.of(context)!;

    // 仅在接近底部时自动滚动，避免打断用户上滑阅读历史
    if (chatState.messages.isNotEmpty && _isNearBottom) {
      _scrollToBottom();
    }

    return Scaffold(
      appBar: AppBar(
        title: Row(
          children: [
            Container(
              width: 10,
              height: 10,
              decoration: BoxDecoration(
                color: isConnected ? Colors.green : Colors.red,
                shape: BoxShape.circle,
              ),
            ),
            const SizedBox(width: 8),
            Text(l10n.chatTitle),
          ],
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.delete_outline),
            tooltip: l10n.clearChat,
            onPressed: chatState.messages.isEmpty
                ? null
                : () {
                    showDialog(
                      context: context,
                      builder: (ctx) => AlertDialog(
                        title: Text(l10n.clearChat),
                        content: Text(l10n.clearChatConfirm),
                        actions: [
                          TextButton(
                            onPressed: () => Navigator.of(ctx).pop(),
                            child: Text(l10n.cancel),
                          ),
                          FilledButton(
                            onPressed: () {
                              ref.read(chatProvider.notifier).clear();
                              Navigator.of(ctx).pop();
                            },
                            child: Text(l10n.clearChat),
                          ),
                        ],
                      ),
                    );
                  },
          ),
          IconButton(
            icon: const Icon(Icons.history),
            tooltip: l10n.sessionList,
            onPressed: () {
              Navigator.of(context).push(
                MaterialPageRoute(builder: (_) => const SessionListScreen()),
              );
            },
          ),
          IconButton(
            icon: const Icon(Icons.settings_outlined),
            tooltip: l10n.settings,
            onPressed: () {
              Navigator.of(context).push(
                MaterialPageRoute(builder: (_) => const SettingsScreen()),
              );
            },
          ),
        ],
      ),
      body: Column(
        children: [
          if (chatState.error != null)
            Container(
              width: double.infinity,
              color: Theme.of(context).colorScheme.errorContainer,
              padding: const EdgeInsets.all(12),
              child: Text(
                chatState.error!,
                style: TextStyle(
                  color: Theme.of(context).colorScheme.onErrorContainer,
                ),
              ),
            ),
          Expanded(
            child: chatState.messages.isEmpty
                ? _buildEmptyState(context, l10n)
                // 使用 SelectionArea 包裹整个 ListView，
                // 用户可以长按选择任意消息的部分内容进行复制
                : SelectionArea(
                    child: ListView.builder(
                      controller: _scrollController,
                      padding: const EdgeInsets.symmetric(
                        horizontal: 12,
                        vertical: 8,
                      ),
                      itemCount: chatState.messages.length,
                      itemBuilder: (context, index) {
                        final msg = chatState.messages[index];
                        return _MessageBubble(message: msg);
                      },
                    ),
                  ),
          ),
          const _InputBar(),
        ],
      ),
    );
  }

  Widget _buildEmptyState(BuildContext context, AppLocalizations l10n) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            Icons.chat_bubble_outline,
            size: 64,
            color: Theme.of(context).colorScheme.outlineVariant,
          ),
          const SizedBox(height: 16),
          Text(
            l10n.startChat,
            style: Theme.of(context).textTheme.titleMedium?.copyWith(
                  color: Theme.of(context).colorScheme.outline,
                ),
          ),
          const SizedBox(height: 8),
          Text(
            l10n.inputBelow,
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: Theme.of(context).colorScheme.outlineVariant,
                ),
          ),
        ],
      ),
    );
  }
}

/// 消息气泡 — 支持长按复制整条消息 + 文本选择复制部分内容
class _MessageBubble extends StatelessWidget {
  final ChatMessage message;

  const _MessageBubble({required this.message});

  /// 复制整条消息内容到剪贴板
  void _copyMessage(BuildContext context) {
    final l10n = AppLocalizations.of(context)!;
    Clipboard.setData(ClipboardData(text: message.content));
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(l10n.copySuccess),
        duration: const Duration(seconds: 2),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final isUser = message.role == MessageRole.user;
    final theme = Theme.of(context);
    final l10n = AppLocalizations.of(context)!;

    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 4),
        constraints: BoxConstraints(
          maxWidth: MediaQuery.of(context).size.width * 0.8,
        ),
        decoration: BoxDecoration(
          color: isUser
              ? theme.colorScheme.primaryContainer
              : theme.colorScheme.surfaceContainerHighest,
          borderRadius: BorderRadius.only(
            topLeft: const Radius.circular(16),
            topRight: const Radius.circular(16),
            bottomLeft: Radius.circular(isUser ? 16 : 4),
            bottomRight: Radius.circular(isUser ? 4 : 16),
          ),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // 顶部操作栏 — 长按消息或点击复制按钮
            if (message.content.isNotEmpty)
              PopupMenuButton<String>(
                icon: Icon(
                  Icons.more_horiz,
                  size: 18,
                  color: isUser
                      ? theme.colorScheme.onPrimaryContainer.withOpacity(0.6)
                      : theme.colorScheme.onSurfaceVariant.withOpacity(0.6),
                ),
                padding: const EdgeInsets.only(top: 4, right: 4),
                tooltip: l10n.copy,
                itemBuilder: (context) => [
                  PopupMenuItem(
                    value: 'copy',
                    child: Row(
                      children: [
                        const Icon(Icons.copy, size: 18),
                        const SizedBox(width: 8),
                        Text(l10n.copy),
                      ],
                    ),
                  ),
                ],
                onSelected: (value) {
                  if (value == 'copy') _copyMessage(context);
                },
              ),
            // 消息内容
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 4),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (message.thinking != null && message.thinking!.isNotEmpty)
                    _ThinkingExpansion(
                      thinking: message.thinking!,
                      isStreaming: message.isStreaming == true && message.content.isEmpty,
                    ),
                  if (message.content.isNotEmpty)
                    MarkdownBody(
                      data: message.content,
                      selectable: true,
                      styleSheet: MarkdownStyleSheet.fromTheme(theme).copyWith(
                        p: theme.textTheme.bodyMedium?.copyWith(
                          color: isUser
                              ? theme.colorScheme.onPrimaryContainer
                              : theme.colorScheme.onSurfaceVariant,
                        ),
                      ),
                    )
                  // 只在既无思考内容又无回复内容时显示"思考中..."占位
                  // 有思考内容时由 _ThinkingExpansion 展示，不重复显示转圈
                  else if (message.isStreaming == true &&
                      (message.thinking == null || message.thinking!.isEmpty))
                    Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(
                            strokeWidth: 2,
                            color: isUser
                                ? theme.colorScheme.onPrimaryContainer
                                : theme.colorScheme.onSurfaceVariant,
                          ),
                        ),
                        const SizedBox(width: 8),
                        Text(
                          l10n.thinking,
                          style: theme.textTheme.bodySmall?.copyWith(
                            color: isUser
                                ? theme.colorScheme.onPrimaryContainer
                                : theme.colorScheme.onSurfaceVariant,
                          ),
                        ),
                      ],
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ThinkingExpansion extends StatefulWidget {
  final String thinking;
  final bool isStreaming; // 是否正在流式接收思考内容

  const _ThinkingExpansion({required this.thinking, this.isStreaming = false});

  @override
  State<_ThinkingExpansion> createState() => _ThinkingExpansionState();
}

class _ThinkingExpansionState extends State<_ThinkingExpansion> {
  bool _expanded = false;
  bool _userToggled = false; // 用户是否手动切换过展开状态
  final ScrollController _scrollController = ScrollController();

  @override
  void didUpdateWidget(_ThinkingExpansion oldWidget) {
    super.didUpdateWidget(oldWidget);
    // 首次收到内容时自动展开（除非用户手动收起过）
    if (!_userToggled && widget.thinking.isNotEmpty && !_expanded) {
      _expanded = true;
    }
    // 流式更新时滚动到底部
    if (_expanded && widget.thinking != oldWidget.thinking) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (_scrollController.hasClients) {
          _scrollController.animateTo(
            _scrollController.position.maxScrollExtent,
            duration: const Duration(milliseconds: 100),
            curve: Curves.easeOut,
          );
        }
      });
    }
  }

  @override
  void dispose() {
    _scrollController.dispose();
    super.dispose();
  }

  void _toggle() {
    setState(() {
      _expanded = !_expanded;
      _userToggled = true;
    });
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final l10n = AppLocalizations.of(context)!;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        InkWell(
          onTap: _toggle,
          borderRadius: BorderRadius.circular(8),
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 4, horizontal: 4),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(
                  _expanded ? Icons.expand_less : Icons.expand_more,
                  size: 18,
                  color: theme.colorScheme.primary,
                ),
                const SizedBox(width: 4),
                Text(
                  l10n.thinkingProcess,
                  style: theme.textTheme.labelSmall?.copyWith(
                    color: theme.colorScheme.primary,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                // 流式接收时显示动画指示器
                if (widget.isStreaming) ...[
                  const SizedBox(width: 6),
                  SizedBox(
                    width: 10,
                    height: 10,
                    child: CircularProgressIndicator(
                      strokeWidth: 1.5,
                      color: theme.colorScheme.primary.withOpacity(0.6),
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
        if (_expanded && widget.thinking.isNotEmpty)
          Container(
            width: double.infinity,
            margin: const EdgeInsets.only(top: 4, bottom: 8),
            padding: const EdgeInsets.all(10),
            constraints: const BoxConstraints(maxHeight: 300),
            decoration: BoxDecoration(
              color: theme.colorScheme.surfaceContainerHighest.withOpacity(0.6),
              borderRadius: BorderRadius.circular(8),
              border: Border.all(
                color: theme.colorScheme.outlineVariant.withOpacity(0.5),
              ),
            ),
            child: SingleChildScrollView(
              controller: _scrollController,
              child: SelectableText(
                widget.thinking,
                style: theme.textTheme.bodySmall?.copyWith(
                  color: theme.colorScheme.onSurfaceVariant,
                  fontStyle: FontStyle.italic,
                  height: 1.4,
                ),
              ),
            ),
          ),
      ],
    );
  }
}

class _InputBar extends ConsumerStatefulWidget {
  const _InputBar();

  @override
  ConsumerState<_InputBar> createState() => _InputBarState();
}

class _InputBarState extends ConsumerState<_InputBar> {
  final _controller = TextEditingController();
  final _focusNode = FocusNode();

  // 修复：发送按钮防抖 — 记录上次发送时间，500ms 内重复点击忽略
  DateTime? _lastSendTime;

  @override
  void dispose() {
    _controller.dispose();
    _focusNode.dispose();
    super.dispose();
  }

  void _send() {
    final text = _controller.text.trim();
    if (text.isEmpty) return;
    // 修复：500ms 防抖，防止用户连续快速点击发送按钮
    final now = DateTime.now();
    if (_lastSendTime != null &&
        now.difference(_lastSendTime!).inMilliseconds < 500) {
      return;
    }
    _lastSendTime = now;

    ref.read(chatProvider.notifier).sendMessage(text);
    _controller.clear();
    _focusNode.requestFocus();
  }

  @override
  Widget build(BuildContext context) {
    final chatState = ref.watch(chatProvider);
    final theme = Theme.of(context);
    final l10n = AppLocalizations.of(context)!;

    return SafeArea(
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        decoration: BoxDecoration(
          color: theme.colorScheme.surface,
          border: Border(
            top: BorderSide(color: theme.colorScheme.outlineVariant),
          ),
        ),
        child: Row(
          children: [
            Expanded(
              child: TextField(
                controller: _controller,
                focusNode: _focusNode,
                enabled: !chatState.isLoading,
                textInputAction: TextInputAction.send,
                onSubmitted: (_) => _send(),
                minLines: 1,
                maxLines: 5,
                decoration: InputDecoration(
                  hintText: l10n.inputMessage,
                  filled: true,
                  fillColor: theme.colorScheme.surfaceContainerHighest,
                  contentPadding: const EdgeInsets.symmetric(
                    horizontal: 16,
                    vertical: 10,
                  ),
                  border: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(24),
                    borderSide: BorderSide.none,
                  ),
                ),
              ),
            ),
            const SizedBox(width: 8),
            if (chatState.isLoading)
              IconButton.filledTonal(
                onPressed: () => ref.read(chatProvider.notifier).cancelStream(),
                icon: const Icon(Icons.stop),
                tooltip: l10n.stopGenerate,
              )
            else
              IconButton.filled(
                onPressed: _send,
                icon: const Icon(Icons.send),
                tooltip: l10n.send,
              ),
          ],
        ),
      ),
    );
  }
}
