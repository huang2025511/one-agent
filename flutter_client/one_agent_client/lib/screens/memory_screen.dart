import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/memory_provider.dart';
import '../models/memory.dart';

/// 记忆管理页面
class MemoryScreen extends ConsumerWidget {
  const MemoryScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final memoryState = ref.watch(memoryProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('记忆管理'),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: '刷新',
            onPressed: () => ref.read(memoryProvider.notifier).loadPage(),
          ),
        ],
      ),
      body: Column(
        children: [
          _SearchBar(),
          Expanded(
            child: _buildBody(context, ref, memoryState),
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton(
        heroTag: 'memory_add',
        tooltip: '添加记忆',
        onPressed: () => _showAddDialog(context, ref),
        child: const Icon(Icons.add),
      ),
    );
  }

  Widget _buildBody(BuildContext context, WidgetRef ref, MemoryState state) {
    if (state.isLoading && state.memories.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }

    if (state.error != null && state.memories.isEmpty) {
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
              onPressed: () => ref.read(memoryProvider.notifier).loadPage(),
              child: const Text('重试'),
            ),
          ],
        ),
      );
    }

    if (state.memories.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.memory_outlined,
              size: 64,
              color: Theme.of(context).colorScheme.outlineVariant,
            ),
            const SizedBox(height: 16),
            Text(
              state.searchQuery.isEmpty ? '暂无记忆' : '未找到相关记忆',
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
      itemCount: state.memories.length,
      itemBuilder: (context, index) {
        final memory = state.memories[index];
        return _MemoryListTile(memory: memory);
      },
    );
  }

  void _showAddDialog(BuildContext context, WidgetRef ref) {
    // 修复：把 controller 放在 stateful 子组件中，对话框关闭时自动释放
    showDialog(
      context: context,
      builder: (ctx) => _AddMemoryDialog(ref: ref),
    );
  }
}

/// 添加记忆对话框（独立 StatefulWidget，确保 controller 在 dispose 时释放）
class _AddMemoryDialog extends ConsumerStatefulWidget {
  final WidgetRef ref;
  const _AddMemoryDialog({required this.ref});

  @override
  ConsumerState<_AddMemoryDialog> createState() => _AddMemoryDialogState();
}

class _AddMemoryDialogState extends ConsumerState<_AddMemoryDialog> {
  final _textController = TextEditingController();
  final _tagsController = TextEditingController();

  @override
  void dispose() {
    _textController.dispose();
    _tagsController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('添加记忆'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          TextField(
            controller: _textController,
            maxLines: 4,
            decoration: const InputDecoration(
              hintText: '记忆内容',
              border: OutlineInputBorder(),
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _tagsController,
            decoration: const InputDecoration(
              hintText: '标签（可选，逗号分隔）',
              border: OutlineInputBorder(),
            ),
          ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('取消'),
        ),
        FilledButton(
          onPressed: () async {
            final text = _textController.text.trim();
            if (text.isEmpty) return;
            final ok = await ref
                .read(memoryProvider.notifier)
                .add(text, tags: _tagsController.text.trim());
            if (context.mounted) {
              Navigator.of(context).pop();
            }
            if (context.mounted) {
              ScaffoldMessenger.of(context).showSnackBar(
                SnackBar(
                  content: Text(ok ? '添加成功' : '添加失败'),
                ),
              );
            }
          },
          child: const Text('添加'),
        ),
      ],
    );
  }
}

class _SearchBar extends ConsumerStatefulWidget {
  @override
  ConsumerState<_SearchBar> createState() => _SearchBarState();
}

class _SearchBarState extends ConsumerState<_SearchBar> {
  final _controller = TextEditingController();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  void _search() {
    final query = _controller.text.trim();
    if (query.isNotEmpty) {
      ref.read(memoryProvider.notifier).search(query);
    } else {
      ref.read(memoryProvider.notifier).loadPage();
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Padding(
      padding: const EdgeInsets.all(12),
      child: SearchBar(
        controller: _controller,
        hintText: '搜索记忆...',
        leading: const Icon(Icons.search),
        trailing: [
          if (_controller.text.isNotEmpty)
            IconButton(
              icon: const Icon(Icons.clear),
              onPressed: () {
                _controller.clear();
                ref.read(memoryProvider.notifier).loadPage();
              },
            ),
        ],
        onSubmitted: (_) => _search(),
        onTapOutside: (_) => FocusScope.of(context).unfocus(),
        backgroundColor: WidgetStatePropertyAll(
          theme.colorScheme.surfaceContainerHighest,
        ),
        elevation: const WidgetStatePropertyAll(0),
      ),
    );
  }
}

class _MemoryListTile extends StatelessWidget {
  final Memory memory;

  const _MemoryListTile({required this.memory});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              memory.text,
              style: theme.textTheme.bodyMedium,
              maxLines: 3,
              overflow: TextOverflow.ellipsis,
            ),
            const SizedBox(height: 8),
            Row(
              children: [
                if (memory.relevance != null)
                  Chip(
                    label: Text('相关度 ${(memory.relevance! * 100).toStringAsFixed(1)}%'),
                    padding: EdgeInsets.zero,
                    visualDensity: VisualDensity.compact,
                    backgroundColor: theme.colorScheme.primaryContainer,
                    labelStyle: theme.textTheme.labelSmall?.copyWith(
                      color: theme.colorScheme.onPrimaryContainer,
                    ),
                  ),
                const Spacer(),
                if (memory.source != null)
                  Text(
                    memory.source!,
                    style: theme.textTheme.labelSmall?.copyWith(
                      color: theme.colorScheme.outline,
                    ),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
