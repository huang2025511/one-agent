import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/memory_provider.dart';
import '../providers/role_provider.dart';
import '../models/memory.dart';
import '../models/role.dart';

/// 记忆管理页面（含「记忆」和「角色」两个 Tab）
class MemoryScreen extends ConsumerStatefulWidget {
  const MemoryScreen({super.key});

  @override
  ConsumerState<MemoryScreen> createState() => _MemoryScreenState();
}

class _MemoryScreenState extends ConsumerState<MemoryScreen>
    with SingleTickerProviderStateMixin {
  late TabController _tabController;

  @override
  void initState() {
    super.initState();
    _tabController = TabController(length: 2, vsync: this);
  }

  @override
  void dispose() {
    _tabController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('记忆 & 角色'),
        bottom: TabBar(
          controller: _tabController,
          tabs: const [
            Tab(icon: Icon(Icons.memory), text: '记忆'),
            Tab(icon: Icon(Icons.person), text: '角色'),
          ],
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: '刷新',
            onPressed: () {
              if (_tabController.index == 0) {
                ref.read(memoryProvider.notifier).loadPage();
              } else {
                ref.read(roleProvider.notifier).load();
              }
            },
          ),
        ],
      ),
      body: TabBarView(
        controller: _tabController,
        children: const [
          _MemoryTab(),
          _RoleTab(),
        ],
      ),
    );
  }
}

// ═══════════════════════════════════════════════════════════════════
// 记忆 Tab
// ═══════════════════════════════════════════════════════════════════

class _MemoryTab extends ConsumerWidget {
  const _MemoryTab();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final memoryState = ref.watch(memoryProvider);

    return Stack(
      children: [
        Column(
          children: [
            _SearchBar(),
            Expanded(child: _buildBody(context, ref, memoryState)),
          ],
        ),
        Positioned(
          right: 16,
          bottom: 16,
          child: FloatingActionButton(
            heroTag: 'memory_add',
            tooltip: '添加记忆',
            onPressed: () => _showAddDialog(context, ref),
            child: const Icon(Icons.add),
          ),
        ),
      ],
    );
  }

  void _showAddDialog(BuildContext context, WidgetRef ref) {
    showDialog(
      context: context,
      builder: (ctx) => _AddMemoryDialog(ref: ref),
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
            Icon(Icons.error_outline, size: 48,
                 color: Theme.of(context).colorScheme.error),
            const SizedBox(height: 12),
            Text('加载失败', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 4),
            Text(state.error!, style: Theme.of(context).textTheme.bodySmall,
                 textAlign: TextAlign.center),
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
            Icon(Icons.memory_outlined, size: 64,
                 color: Theme.of(context).colorScheme.outlineVariant),
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
      itemBuilder: (context, index) =>
          _MemoryListTile(memory: state.memories[index]),
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
            Text(memory.text, style: theme.textTheme.bodyMedium,
                 maxLines: 3, overflow: TextOverflow.ellipsis),
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
                  Text(memory.source!,
                       style: theme.textTheme.labelSmall
                           ?.copyWith(color: theme.colorScheme.outline)),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

// ═══════════════════════════════════════════════════════════════════
// 角色 Tab
// ═══════════════════════════════════════════════════════════════════

class _RoleTab extends ConsumerStatefulWidget {
  const _RoleTab();

  @override
  ConsumerState<_RoleTab> createState() => _RoleTabState();
}

class _RoleTabState extends ConsumerState<_RoleTab> {
  bool _wasConnected = false;

  @override
  void initState() {
    super.initState();
    // 延迟加载，避免在 build 中直接触发
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _maybeLoad();
    });
  }

  @override
  Widget build(BuildContext context) {
    // 问题9 修复：监听连接状态变化，连接成功后自动刷新角色列表
    // 之前只在 initState 加载一次，断开后重连不会刷新，导致角色列表过期
    final isConnected = ref.watch(settingsProvider.select((s) => s.isConnected));
    if (isConnected && !_wasConnected) {
      _wasConnected = true;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        ref.read(roleProvider.notifier).load();
      });
    } else if (!isConnected) {
      _wasConnected = false;
    }

    final state = ref.watch(roleProvider);
    return Stack(
      children: [
        _buildBody(context, state, isConnected),
        Positioned(
          right: 16,
          bottom: 16,
          child: FloatingActionButton(
            heroTag: 'role_add',
            tooltip: '添加角色',
            onPressed: isConnected
                ? () => _showEditDialog(context, ref, null)
                : null,
            child: const Icon(Icons.add),
          ),
        ),
      ],
    );
  }

  void _maybeLoad() {
    final isConnected = ref.read(settingsProvider).isConnected;
    if (isConnected) {
      _wasConnected = true;
      ref.read(roleProvider.notifier).load();
    }
  }

  Widget _buildBody(BuildContext context, RoleState state, bool isConnected) {
    // 问题9 修复：未连接时显示提示，而不是显示过期/空的角色列表
    if (!isConnected) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.cloud_off, size: 56,
                 color: Theme.of(context).colorScheme.outlineVariant),
            const SizedBox(height: 16),
            Text('未连接服务器',
                style: Theme.of(context).textTheme.titleMedium?.copyWith(
                  color: Theme.of(context).colorScheme.outline,
                )),
            const SizedBox(height: 8),
            Text('角色数据由服务端统一管理，请先连接 One-Agent 服务器',
                textAlign: TextAlign.center,
                style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: Theme.of(context).colorScheme.outline,
                )),
          ],
        ),
      );
    }
    if (state.isLoading && state.roles.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    if (state.error != null && state.roles.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.error_outline, size: 48,
                 color: Theme.of(context).colorScheme.error),
            const SizedBox(height: 12),
            Text('加载失败', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 4),
            Text(state.error!, style: Theme.of(context).textTheme.bodySmall,
                 textAlign: TextAlign.center),
            const SizedBox(height: 16),
            FilledButton.tonal(
              onPressed: () => ref.read(roleProvider.notifier).load(),
              child: const Text('重试'),
            ),
          ],
        ),
      );
    }
    if (state.roles.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.person_outline, size: 64,
                 color: Theme.of(context).colorScheme.outlineVariant),
            const SizedBox(height: 16),
            Text('暂无角色', style: Theme.of(context).textTheme.titleMedium?.copyWith(
                  color: Theme.of(context).colorScheme.outline,
                )),
            const SizedBox(height: 8),
            Text('点击右下角 + 创建角色\n角色可以自定义 Agent 的人格和行为',
                 textAlign: TextAlign.center,
                 style: Theme.of(context).textTheme.bodySmall?.copyWith(
                   color: Theme.of(context).colorScheme.outline,
                 )),
          ],
        ),
      );
    }
    // 分组：内置角色 / 自定义角色
    final builtinRoles = state.roles.where((r) => r.isBuiltin).toList();
    final customRoles = state.roles.where((r) => !r.isBuiltin).toList();

    // 问题9 修复：添加下拉刷新，用户可手动同步服务端最新角色
    return RefreshIndicator(
      onRefresh: () => ref.read(roleProvider.notifier).load(),
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(8, 8, 8, 80),
        children: [
          // 顶部说明文字
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            child: Text(
              '选择一个角色激活，或创建自定义角色\n（下拉刷新同步服务端最新角色）',
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                    color: Theme.of(context).colorScheme.onSurfaceVariant,
                  ),
            ),
          ),
          if (builtinRoles.isNotEmpty) ...[
            _buildSectionHeader(context, '内置角色'),
            ...builtinRoles.map((r) => _RoleListTile(role: r)),
          ],
          if (builtinRoles.isNotEmpty && customRoles.isNotEmpty)
            const Divider(height: 32),
          if (customRoles.isNotEmpty) ...[
            _buildSectionHeader(context, '自定义角色'),
            ...customRoles.map((r) => _RoleListTile(role: r)),
          ],
        ],
      ),
    );
  }

  Widget _buildSectionHeader(BuildContext context, String title) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      child: Text(
        title,
        style: Theme.of(context).textTheme.titleSmall?.copyWith(
              color: Theme.of(context).colorScheme.primary,
              fontWeight: FontWeight.w600,
            ),
      ),
    );
  }

  void _showEditDialog(BuildContext context, WidgetRef ref, Role? existing) {
    showDialog(
      context: context,
      builder: (ctx) => _RoleEditDialog(ref: ref, existing: existing),
    );
  }
}

class _RoleListTile extends ConsumerWidget {
  final Role role;
  const _RoleListTile({required this.role});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
      child: ListTile(
        contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        leading: CircleAvatar(
          backgroundColor: _parseColor(role.color, theme),
          child: Text(role.icon, style: const TextStyle(fontSize: 20)),
        ),
        title: Row(
          children: [
            Flexible(
              child: Text(role.name,
                  style: theme.textTheme.titleSmall,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis),
            ),
            if (role.isBuiltin) ...[
              const SizedBox(width: 8),
              Chip(
                label: const Text('内置'),
                padding: EdgeInsets.zero,
                visualDensity: VisualDensity.compact,
                materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                backgroundColor: Colors.blue,
                labelStyle: theme.textTheme.labelSmall?.copyWith(
                  color: Colors.white,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
            if (role.isActive) ...[
              const SizedBox(width: 8),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                decoration: BoxDecoration(
                  color: theme.colorScheme.primaryContainer,
                  borderRadius: BorderRadius.circular(4),
                ),
                child: Text('活跃', style: theme.textTheme.labelSmall?.copyWith(
                  color: theme.colorScheme.onPrimaryContainer,
                  fontWeight: FontWeight.w600,
                )),
              ),
            ],
          ],
        ),
        subtitle: role.description.isNotEmpty
            ? Text(role.description, maxLines: 2, overflow: TextOverflow.ellipsis,
                   style: theme.textTheme.bodySmall)
            : null,
        trailing: PopupMenuButton<String>(
          onSelected: (action) async {
            switch (action) {
              case 'activate':
                await ref.read(roleProvider.notifier).activate(role.id);
                if (context.mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(
                    SnackBar(content: Text('已激活角色：${role.name}')),
                  );
                }
              case 'deactivate':
                await ref.read(roleProvider.notifier).deactivate();
              case 'edit':
                showDialog(
                  context: context,
                  builder: (ctx) => _RoleEditDialog(ref: ref, existing: role),
                );
              case 'delete':
                final ok = await _confirmDelete(context);
                if (ok == true && context.mounted) {
                  await ref.read(roleProvider.notifier).delete(role.id);
                }
            }
          },
          itemBuilder: (context) => [
            if (!role.isActive)
              const PopupMenuItem(value: 'activate', child: Text('激活')),
            if (role.isActive)
              const PopupMenuItem(value: 'deactivate', child: Text('取消激活')),
            const PopupMenuItem(value: 'edit', child: Text('编辑')),
            if (!role.isBuiltin)
              const PopupMenuItem(value: 'delete', child: Text('删除')),
          ],
        ),
        onTap: () => showDialog(
          context: context,
          builder: (ctx) => _RoleEditDialog(ref: ref, existing: role),
        ),
      ),
    );
  }

  Color _parseColor(String hex, ThemeData theme) {
    try {
      final c = hex.replaceAll('#', '');
      return Color(int.parse('FF$c', radix: 16));
    } catch (_) {
      return theme.colorScheme.primaryContainer;
    }
  }

  Future<bool?> _confirmDelete(BuildContext context) {
    return showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('删除角色'),
        content: Text('确定要删除角色「${role.name}」吗？此操作不可撤销。'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('取消')),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: FilledButton.styleFrom(
              backgroundColor: Theme.of(context).colorScheme.error,
            ),
            child: const Text('删除'),
          ),
        ],
      ),
    );
  }
}

/// 创建/编辑角色对话框
class _RoleEditDialog extends ConsumerStatefulWidget {
  final WidgetRef ref;
  final Role? existing;
  const _RoleEditDialog({required this.ref, this.existing});

  @override
  ConsumerState<_RoleEditDialog> createState() => _RoleEditDialogState();
}

class _RoleEditDialogState extends ConsumerState<_RoleEditDialog> {
  final _nameController = TextEditingController();
  final _descController = TextEditingController();
  final _promptController = TextEditingController();
  final _iconController = TextEditingController();
  final _colorController = TextEditingController();

  static const _presetIcons = ['🤖', '🐱', '🦊', '🐼', '🧙', '👨‍💻', '🎨', '📚', '🔬', '⚖️'];
  static const _presetColors = ['#6750A4', '#E91E63', '#2196F3', '#4CAF50', '#FF9800', '#795548'];

  @override
  void initState() {
    super.initState();
    if (widget.existing != null) {
      _nameController.text = widget.existing!.name;
      _descController.text = widget.existing!.description;
      _promptController.text = widget.existing!.systemPromptOverride;
      _iconController.text = widget.existing!.icon;
      _colorController.text = widget.existing!.color;
    } else {
      _iconController.text = '🤖';
      _colorController.text = '#6750A4';
    }
  }

  @override
  void dispose() {
    _nameController.dispose();
    _descController.dispose();
    _promptController.dispose();
    _iconController.dispose();
    _colorController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final isEditing = widget.existing != null;
    return AlertDialog(
      title: Text(isEditing ? '编辑角色' : '创建角色'),
      content: SizedBox(
        width: double.maxFinite,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              // 图标选择
              Align(
                alignment: Alignment.centerLeft,
                child: Text('图标', style: Theme.of(context).textTheme.labelMedium),
              ),
              const SizedBox(height: 4),
              Wrap(
                spacing: 8,
                children: _presetIcons.map((icon) {
                  final selected = _iconController.text == icon;
                  return GestureDetector(
                    onTap: () => setState(() => _iconController.text = icon),
                    child: Container(
                      width: 36, height: 36,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        border: Border.all(
                          color: selected
                              ? Theme.of(context).colorScheme.primary
                              : Colors.transparent,
                          width: 2,
                        ),
                        color: Theme.of(context).colorScheme.surfaceContainerHighest,
                      ),
                      child: Center(child: Text(icon, style: const TextStyle(fontSize: 18))),
                    ),
                  );
                }).toList(),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _nameController,
                decoration: const InputDecoration(
                  labelText: '角色名称',
                  hintText: '如：翻译官、代码审查员',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _descController,
                maxLines: 2,
                decoration: const InputDecoration(
                  labelText: '描述（可选）',
                  hintText: '简单说明这个角色的用途',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _promptController,
                maxLines: 6,
                decoration: const InputDecoration(
                  labelText: '系统提示词',
                  hintText: '定义角色的行为模式...\n\n例如：\n你是一个中英翻译官，所有回复都包含中英双语。用户输入中文时翻译为英文，输入英文时翻译为中文。',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 12),
              // 颜色选择
              Align(
                alignment: Alignment.centerLeft,
                child: Text('颜色', style: Theme.of(context).textTheme.labelMedium),
              ),
              const SizedBox(height: 4),
              Wrap(
                spacing: 8,
                children: _presetColors.map((hex) {
                  final selected = _colorController.text == hex;
                  return GestureDetector(
                    onTap: () => setState(() => _colorController.text = hex),
                    child: Container(
                      width: 32, height: 32,
                      decoration: BoxDecoration(
                        shape: BoxShape.circle,
                        color: _parseColor(hex),
                        border: Border.all(
                          color: selected ? Theme.of(context).colorScheme.primary : Colors.transparent,
                          width: 3,
                        ),
                      ),
                    ),
                  );
                }).toList(),
              ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('取消'),
        ),
        FilledButton(
          onPressed: () async {
            final name = _nameController.text.trim();
            if (name.isEmpty) return;
            final notifier = ref.read(roleProvider.notifier);
            bool ok;
            if (isEditing) {
              ok = await notifier.update(
                widget.existing!.id,
                name: name,
                description: _descController.text.trim(),
                systemPromptOverride: _promptController.text.trim(),
                icon: _iconController.text,
                color: _colorController.text,
              );
            } else {
              ok = await notifier.create(
                name: name,
                description: _descController.text.trim(),
                systemPromptOverride: _promptController.text.trim(),
                icon: _iconController.text,
                color: _colorController.text,
              );
            }
            if (context.mounted) {
              Navigator.of(context).pop();
              ScaffoldMessenger.of(context).showSnackBar(
                SnackBar(content: Text(ok ? '保存成功' : '保存失败')),
              );
            }
          },
          child: Text(isEditing ? '保存' : '创建'),
        ),
      ],
    );
  }

  Color _parseColor(String hex) {
    try {
      final c = hex.replaceAll('#', '');
      return Color(int.parse('FF$c', radix: 16));
    } catch (_) {
      return Colors.purple;
    }
  }
}

// ═══════════════════════════════════════════════════════════════════
// 添加记忆对话框
// ═══════════════════════════════════════════════════════════════════

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
                SnackBar(content: Text(ok ? '添加成功' : '添加失败')),
              );
            }
          },
          child: const Text('添加'),
        ),
      ],
    );
  }
}
