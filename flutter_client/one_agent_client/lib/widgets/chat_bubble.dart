import 'package:flutter/material.dart';
import 'package:flutter_markdown/flutter_markdown.dart';

import '../models/chat_message.dart';
import 'thinking_card.dart';

/// 聊天消息气泡
class ChatBubble extends StatelessWidget {
  final ChatMessage message;
  final bool showThinking;

  const ChatBubble({
    super.key,
    required this.message,
    this.showThinking = true,
  });

  @override
  Widget build(BuildContext context) {
    final isUser = message.role == MessageRole.user;
    final isThinking = message.role == MessageRole.thinking;
    final isTool = message.role == MessageRole.tool;

    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: EdgeInsets.only(
          left: isUser ? 64 : 12,
          right: isUser ? 12 : 64,
          top: 4,
          bottom: 4,
        ),
        child: Column(
          crossAxisAlignment:
              isUser ? CrossAxisAlignment.end : CrossAxisAlignment.start,
          children: [
            // 角色标签
            if (!isUser && !isThinking)
              Padding(
                padding: const EdgeInsets.only(left: 4, bottom: 2),
                child: Text(
                  isTool ? '工具' : 'One-Agent',
                  style: TextStyle(
                    fontSize: 11,
                    color: Theme.of(context).colorScheme.outline,
                  ),
                ),
              ),

            // 思考过程卡片
            if (showThinking && message.thinking != null && message.thinking!.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: ThinkingCard(thinking: message.thinking!),
              ),

            // 消息内容气泡
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
              decoration: BoxDecoration(
                color: isUser
                    ? Theme.of(context).colorScheme.primaryContainer
                    : isThinking
                        ? Colors.amber.shade50
                        : Theme.of(context).colorScheme.surfaceContainerHighest,
                borderRadius: BorderRadius.only(
                  topLeft: const Radius.circular(18),
                  topRight: const Radius.circular(18),
                  bottomLeft: Radius.circular(isUser ? 18 : 4),
                  bottomRight: Radius.circular(isUser ? 4 : 18),
                ),
              ),
              child: _buildContent(context),
            ),

            // 时间戳
            Padding(
              padding: const EdgeInsets.only(top: 2, left: 4, right: 4),
              child: Text(
                _formatTime(message.timestamp),
                style: TextStyle(
                  fontSize: 10,
                  color: Theme.of(context).colorScheme.outline,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildContent(BuildContext context) {
    final isUser = message.role == MessageRole.user;
    if (message.isStreaming == true && message.content.isEmpty) {
      return SizedBox(
        width: 40,
        child: LinearProgressIndicator(
          backgroundColor: Colors.transparent,
          color: Theme.of(context).colorScheme.primary.withOpacity(0.5),
        ),
      );
    }

    if (message.isError == true) {
      return Text(
        message.errorMessage ?? '发生错误',
        style: TextStyle(color: Theme.of(context).colorScheme.error),
      );
    }

    // 使用 Markdown 渲染助手消息
    if (message.role == MessageRole.assistant) {
      return MarkdownBody(
        data: message.content,
        selectable: true,
        styleSheet: MarkdownStyleSheet(
          p: TextStyle(
            fontSize: 15,
            height: 1.5,
            color: Theme.of(context).colorScheme.onSurface,
          ),
          code: TextStyle(
            fontSize: 13,
            backgroundColor: Theme.of(context).colorScheme.surfaceContainerHighest,
            fontFamily: 'monospace',
          ),
          codeblockDecoration: BoxDecoration(
            color: Theme.of(context).colorScheme.surfaceContainerHighest,
            borderRadius: BorderRadius.circular(8),
          ),
        ),
      );
    }

    return Text(
      message.content,
      style: TextStyle(
        fontSize: 15,
        height: 1.5,
        color: isUser
            ? Theme.of(context).colorScheme.onPrimaryContainer
            : Theme.of(context).colorScheme.onSurface,
      ),
    );
  }

  String _formatTime(DateTime? dt) {
    if (dt == null) return '';
    return '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';
  }
}
