import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_slidable/flutter_slidable.dart';
import 'package:intl/intl.dart';

import '../providers/session_provider.dart';
import '../providers/chat_provider.dart';
import '../models/session.dart';

/// 会话列表页面
class SessionListScreen extends ConsumerStatefulWidget {
  const SessionListScreen({super.key});

  @override
  ConsumerState<SessionListScreen> createState() => _SessionListScreenState();
}

class _SessionListScreenState extends ConsumerState<SessionListScreen> {
  @override
  void initState() {
    super.initState();
    // 打开页面时自动加载会话列表
    WidgetsBinding.instance.addPostFrameCallback((_) {
      ref.read(sessionListProvider.notifier).load();
    });
  }

  @override
  Widget build(BuildContext context) {
    final sessionState = ref.watch(sessionListProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('会话列表'),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: '刷新',
            onPressed: () => ref.read(sessionListProvider.notifier).load(),
          ),
        ],
      ),
      body: _buildBody(context, ref, sessionState),
    );
  }

  Widget _buildBody(
    BuildContext context,
    WidgetRef ref,
    SessionListState state,
  ) {
    if (state.isLoading && state.sessions.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }

    if (state.error != null && state.sessions.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.error_outline,
              size: 48,
              color: Theme.of(context).colorScheme.error,
            ),
            const SizedBox(height: 12),
            Text('加载失败', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 4),
            Text(
              state.error!,
              style: Theme.of(context).textTheme.bodySmall,
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 16),
            FilledButton.tonal(
              onPressed: () => ref.read(sessionListProvider.notifier).load(),
              child: const Text('重试'),
            ),
          ],
        ),
      );
    }

    if (state.sessions.isEmpty) {
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
              '暂无会话',
              style: Theme.of(context).textTheme.titleMedium?.copyWith(
                    color: Theme.of(context).colorScheme.outline,
                  ),
            ),
          ],
        ),
      );
    }

    return ListView.builder(
      padding: const EdgeInsets.symmetric(vertical: 8),
      itemCount: state.sessions.length,
      itemBuilder: (context, index) {
        final session = state.sessions[index];
        return _SessionListTile(session: session);
      },
    );
  }
}

class _SessionListTile extends ConsumerWidget {
  final Session session;

  const _SessionListTile({required this.session});

  String _formatTime(DateTime? dt) {
    if (dt == null) return '';
    return DateFormat('MM-dd HH:mm').format(dt);
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);

    return Slidable(
      key: ValueKey(session.id),
      endActionPane: ActionPane(
        motion: const DrawerMotion(),
        extentRatio: 0.25,
        children: [
          SlidableAction(
            onPressed: (_) async {
              final confirmed = await showDialog<bool>(
                context: context,
                builder: (ctx) => AlertDialog(
                  title: const Text('删除会话'),
                  content: Text('确定要删除会话 "${session.title ?? '未命名'}" 吗？'),
                  actions: [
                    TextButton(
                      onPressed: () => Navigator.of(ctx).pop(false),
                      child: const Text('取消'),
                    ),
                    FilledButton(
                      onPressed: () => Navigator.of(ctx).pop(true),
                      child: const Text('删除'),
                    ),
                  ],
                ),
              );
              if (confirmed == true) {
                await ref
                    .read(sessionListProvider.notifier)
                    .delete(session.id);
              }
            },
            backgroundColor: theme.colorScheme.error,
            foregroundColor: theme.colorScheme.onError,
            icon: Icons.delete,
            label: '删除',
          ),
        ],
      ),
      child: ListTile(
        leading: CircleAvatar(
          backgroundColor: theme.colorScheme.primaryContainer,
          child: Icon(
            Icons.chat_bubble_outline,
            color: theme.colorScheme.onPrimaryContainer,
          ),
        ),
        title: Text(session.title ?? '未命名会话'),
        subtitle: Text(
          '${_formatTime(session.updatedAt)} · ${session.messageCount ?? 0} 条消息',
          style: theme.textTheme.bodySmall,
        ),
        trailing: const Icon(Icons.chevron_right),
        onTap: () {
          ref.read(chatProvider.notifier).setSession(session.id);
          Navigator.of(context).pop();
        },
        onLongPress: () async {
          final newId = await ref
              .read(sessionListProvider.notifier)
              .fork(session.id);
          if (newId != null && context.mounted) {
            ScaffoldMessenger.of(context).showSnackBar(
              const SnackBar(content: Text('Fork 会话成功')),
            );
            ref.read(sessionListProvider.notifier).load();
          }
        },
      ),
    );
  }
}
