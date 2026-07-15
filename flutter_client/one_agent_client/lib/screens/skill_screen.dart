import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart';

import '../api/skill_api.dart';
import '../api/system_api.dart';
import '../models/skill.dart';
import '../providers/skill_provider.dart';

/// 技能管理页面
class SkillScreen extends ConsumerStatefulWidget {
  const SkillScreen({super.key});

  @override
  ConsumerState<SkillScreen> createState() => _SkillScreenState();
}

class _SkillScreenState extends ConsumerState<SkillScreen>
    with SingleTickerProviderStateMixin {
  late TabController _tabController;
  final _searchController = TextEditingController();
  final GlobalKey<_MarketplaceTabState> _marketplaceKey =
      GlobalKey<_MarketplaceTabState>();

  @override
  void initState() {
    super.initState();
    _tabController = TabController(length: 2, vsync: this);
    _tabController.addListener(_onTabChanged);
    // 初始化加载
    WidgetsBinding.instance.addPostFrameCallback((_) {
      ref.read(skillProvider.notifier).loadSkills();
    });
  }

  void _onTabChanged() {
    if (_tabController.index == 1 && !_tabController.indexIsChanging) {
      ref.read(skillProvider.notifier).searchMarketplace('');
    }
  }

  @override
  void dispose() {
    _tabController.dispose();
    _searchController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final skillState = ref.watch(skillProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('技能管理'),
        bottom: TabBar(
          controller: _tabController,
          tabs: const [
            Tab(text: '已安装', icon: Icon(Icons.check_circle_outline)),
            Tab(text: '市场', icon: Icon(Icons.storefront_outlined)),
          ],
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: '刷新',
            onPressed: () {
              if (_tabController.index == 0) {
                ref.read(skillProvider.notifier).loadSkills();
              } else {
                _marketplaceKey.currentState?.refresh();
              }
            },
          ),
        ],
      ),
      body: TabBarView(
        controller: _tabController,
        children: [
          _InstalledTab(state: skillState),
          _MarketplaceTab(
            key: _marketplaceKey,
            state: skillState,
            searchController: _searchController,
          ),
        ],
      ),
    );
  }
}

class _InstalledTab extends ConsumerWidget {
  final SkillState state;

  const _InstalledTab({required this.state});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (state.isLoading && state.skills.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }

    if (state.error != null && state.skills.isEmpty) {
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
              onPressed: () => ref.read(skillProvider.notifier).loadSkills(),
              child: const Text('重试'),
            ),
          ],
        ),
      );
    }

    if (state.skills.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.extension_off_outlined,
              size: 64,
              color: Theme.of(context).colorScheme.outlineVariant,
            ),
            const SizedBox(height: 16),
            Text(
              '暂无已安装技能',
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
      itemCount: state.skills.length,
      itemBuilder: (context, index) {
        final skill = state.skills[index];
        return _SkillListTile(skill: skill);
      },
    );
  }
}

class _SkillListTile extends ConsumerStatefulWidget {
  final Skill skill;

  const _SkillListTile({required this.skill});

  @override
  ConsumerState<_SkillListTile> createState() => _SkillListTileState();
}

class _SkillListTileState extends ConsumerState<_SkillListTile> {
  bool _expanded = false;

  /// 根据技能 directory 字段判断来源标识
  String? _sourceLabel() {
    final dir = widget.skill.directory;
    if (dir == null || dir.isEmpty) return null;
    if (dir.contains('builtin')) return '内置';
    if (dir.contains('user')) return '自定义';
    if (dir.contains('marketplace')) return '市场安装';
    if (dir.contains('procedural')) return '自动学习';
    return null;
  }

  Color? _sourceColor(String label, ThemeData theme) {
    switch (label) {
      case '内置':
        return theme.colorScheme.primaryContainer;
      case '自定义':
        return theme.colorScheme.tertiaryContainer;
      case '市场安装':
        return theme.colorScheme.secondaryContainer;
      case '自动学习':
        return theme.colorScheme.errorContainer;
      default:
        return null;
    }
  }

  String _formatLastUsed(DateTime? dt) {
    if (dt == null) return '';
    return DateFormat('MM-dd HH:mm').format(dt);
  }

  Future<bool?> _confirmUninstall(BuildContext context) {
    final skill = widget.skill;
    return showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('卸载技能'),
        content: Text('确定要卸载技能「${skill.title}」吗？此操作不可撤销。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: FilledButton.styleFrom(
              backgroundColor: Theme.of(ctx).colorScheme.errorContainer,
              foregroundColor: Theme.of(ctx).colorScheme.onErrorContainer,
            ),
            child: const Text('卸载'),
          ),
        ],
      ),
    );
  }

  Future<void> _handleUninstall() async {
    final ok = await _confirmUninstall(context);
    if (ok != true) return;
    if (!mounted) return;
    final success = await SkillApi.uninstall(
      widget.skill.id,
      targetDir: widget.skill.directory,
    );
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(success ? '卸载成功' : '卸载失败')),
    );
    if (success) {
      ref.read(skillProvider.notifier).loadSkills();
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final skill = widget.skill;
    final sourceLabel = _sourceLabel();
    final description = skill.description;
    final hasDescription = description != null && description.isNotEmpty;

    return ListTile(
      leading: CircleAvatar(
        backgroundColor: theme.colorScheme.secondaryContainer,
        child: Icon(
          Icons.extension,
          color: theme.colorScheme.onSecondaryContainer,
        ),
      ),
      title: Row(
        children: [
          Flexible(child: Text(skill.title)),
          if (sourceLabel != null) ...[
            const SizedBox(width: 8),
            Chip(
              label: Text(sourceLabel),
              visualDensity: VisualDensity.compact,
              padding: EdgeInsets.zero,
              materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
              backgroundColor: _sourceColor(sourceLabel, theme),
              labelStyle: theme.textTheme.labelSmall,
            ),
          ],
        ],
      ),
      subtitle: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (hasDescription)
            _ExpandableDescription(
              text: description,
              style: theme.textTheme.bodySmall,
              maxLines: 2,
              expanded: _expanded,
              onToggle: () => setState(() => _expanded = !_expanded),
            )
          else
            Text(
              '无描述',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: theme.textTheme.bodySmall?.copyWith(
                color: theme.colorScheme.outline,
              ),
            ),
          const SizedBox(height: 4),
          Wrap(
            spacing: 6,
            runSpacing: 2,
            children: [
              if (skill.version != null)
                _MetaChip(label: 'v${skill.version}'),
              if (skill.uses != null && skill.uses! > 0)
                _MetaChip(label: '使用 ${skill.uses} 次'),
              if (skill.lastUsed != null)
                _MetaChip(label: '最后使用 ${_formatLastUsed(skill.lastUsed)}'),
            ],
          ),
        ],
      ),
      trailing: PopupMenuButton<String>(
        icon: const Icon(Icons.more_vert),
        tooltip: '操作',
        onSelected: (action) async {
          switch (action) {
            case 'uninstall':
              await _handleUninstall();
              break;
          }
        },
        itemBuilder: (context) => const [
          PopupMenuItem(
            value: 'uninstall',
            child: Row(
              children: [
                Icon(Icons.delete_outline, size: 20),
                SizedBox(width: 8),
                Text('卸载'),
              ],
            ),
          ),
        ],
      ),
      isThreeLine: true,
    );
  }
}

/// 描述超过 maxLines 行时显示"展开/收起"按钮
class _ExpandableDescription extends StatelessWidget {
  final String text;
  final TextStyle? style;
  final int maxLines;
  final bool expanded;
  final VoidCallback? onToggle;

  const _ExpandableDescription({
    required this.text,
    this.style,
    this.maxLines = 2,
    required this.expanded,
    this.onToggle,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return LayoutBuilder(
      builder: (context, constraints) {
        final span = TextSpan(text: text, style: style);
        final tp = TextPainter(
          text: span,
          maxLines: maxLines,
          textDirection: ui.TextDirection.ltr,
        )..layout(maxWidth: constraints.maxWidth);
        final isOverflow = tp.didExceedMaxLines;
        tp.dispose();

        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              text,
              maxLines: expanded ? null : maxLines,
              overflow:
                  expanded ? TextOverflow.visible : TextOverflow.ellipsis,
              style: style,
            ),
            if (isOverflow && onToggle != null)
              GestureDetector(
                onTap: onToggle,
                child: Text(
                  expanded ? '收起' : '展开',
                  style: theme.textTheme.labelSmall?.copyWith(
                    color: theme.colorScheme.primary,
                  ),
                ),
              ),
          ],
        );
      },
    );
  }
}

/// 元信息小标签（版本 / 使用次数 / 最后使用时间）
class _MetaChip extends StatelessWidget {
  final String label;

  const _MetaChip({required this.label});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: theme.colorScheme.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(4),
      ),
      child: Text(
        label,
        style: theme.textTheme.labelSmall?.copyWith(
          color: theme.colorScheme.outline,
        ),
      ),
    );
  }
}

class _MarketplaceTab extends ConsumerStatefulWidget {
  final SkillState state;
  final TextEditingController searchController;

  const _MarketplaceTab({
    super.key,
    required this.state,
    required this.searchController,
  });

  @override
  ConsumerState<_MarketplaceTab> createState() => _MarketplaceTabState();
}

class _MarketplaceTabState extends ConsumerState<_MarketplaceTab>
    with SingleTickerProviderStateMixin {
  late TabController _marketTabController;

  // 公开市场状态
  List<Map<String, dynamic>> _publicPackages = [];
  bool _publicLoading = false;
  String? _publicError;
  bool _publicInitialized = false;

  @override
  void initState() {
    super.initState();
    _marketTabController = TabController(length: 2, vsync: this);
    _marketTabController.addListener(() {
      if (_marketTabController.indexIsChanging) return;
      // 切换到公开市场时按需加载
      if (_marketTabController.index == 1 &&
          !_publicInitialized &&
          !_publicLoading) {
        _browsePublicMarket();
      }
      setState(() {});
    });
  }

  @override
  void dispose() {
    _marketTabController.dispose();
    super.dispose();
  }

  /// 由父级 AppBar 刷新按钮调用
  void refresh() {
    if (_marketTabController.index == 0) {
      ref
          .read(skillProvider.notifier)
          .searchMarketplace(widget.searchController.text.trim());
    } else {
      _browsePublicMarket();
    }
  }

  Future<void> _browsePublicMarket() async {
    setState(() {
      _publicLoading = true;
      _publicError = null;
    });
    try {
      final result = await SystemApi.browseMarketplace(
        query: widget.searchController.text.trim(),
      );
      if (!mounted) return;
      final packages = (result?['packages'] as List<dynamic>? ?? [])
          .map((e) => e is Map<String, dynamic>
              ? e
              : Map<String, dynamic>.from(e as Map))
          .toList();
      setState(() {
        _publicPackages = packages;
        _publicLoading = false;
        _publicInitialized = true;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _publicError = e.toString();
        _publicLoading = false;
        _publicInitialized = true;
      });
    }
  }

  /// 切换到"公开市场"标签并拉取数据
  void _switchToPublicMarket() {
    if (_marketTabController.index != 1) {
      _marketTabController.animateTo(1);
    }
    _browsePublicMarket();
  }

  Future<void> _installFromUrl(String source) async {
    if (source.isEmpty) return;
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(content: Text('正在安装...')),
    );
    try {
      final result = await SystemApi.installFromUrl(source);
      if (!mounted) return;
      final success = result?['success'] == true;
      final message = (result?['message'] as String?) ??
          (success ? '安装成功' : '安装失败');
      ScaffoldMessenger.of(context).hideCurrentSnackBar();
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(message)),
      );
      if (success) {
        // 安装后刷新已安装列表
        ref.read(skillProvider.notifier).loadSkills();
      }
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).hideCurrentSnackBar();
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('安装失败: $e')),
      );
    }
  }

  Future<void> _showCustomInstallDialog() async {
    final controller = TextEditingController();
    final result = await showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('自定义安装'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('输入 GitHub URL 或 "owner/repo/path" 格式的技能源：'),
            const SizedBox(height: 12),
            TextField(
              controller: controller,
              autofocus: true,
              decoration: const InputDecoration(
                hintText: 'owner/repo/path',
                border: OutlineInputBorder(),
              ),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, controller.text.trim()),
            child: const Text('安装'),
          ),
        ],
      ),
    );
    controller.dispose();
    if (result != null && result.isNotEmpty) {
      await _installFromUrl(result);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.all(12),
          child: SearchBar(
            controller: widget.searchController,
            hintText: '搜索技能市场...',
            leading: const Icon(Icons.search),
            trailing: [
              IconButton(
                icon: const Icon(Icons.add_link),
                tooltip: '自定义安装',
                onPressed: _showCustomInstallDialog,
              ),
              IconButton(
                icon: const Icon(Icons.public),
                tooltip: '公开市场',
                onPressed: _switchToPublicMarket,
              ),
              if (widget.searchController.text.isNotEmpty)
                IconButton(
                  icon: const Icon(Icons.clear),
                  onPressed: () {
                    widget.searchController.clear();
                    if (_marketTabController.index == 0) {
                      ref
                          .read(skillProvider.notifier)
                          .searchMarketplace('');
                    } else {
                      _browsePublicMarket();
                    }
                  },
                ),
            ],
            onSubmitted: (value) {
              if (_marketTabController.index == 0) {
                ref
                    .read(skillProvider.notifier)
                    .searchMarketplace(value.trim());
              } else {
                _browsePublicMarket();
              }
            },
            backgroundColor: WidgetStatePropertyAll(
              theme.colorScheme.surfaceContainerHighest,
            ),
            elevation: const WidgetStatePropertyAll(0),
          ),
        ),
        TabBar(
          controller: _marketTabController,
          tabs: const [
            Tab(text: '本地市场'),
            Tab(text: '公开市场'),
          ],
        ),
        Expanded(
          child: TabBarView(
            controller: _marketTabController,
            children: [
              _buildLocalMarketBody(context, ref),
              _buildPublicMarketBody(context),
            ],
          ),
        ),
      ],
    );
  }

  Widget _buildLocalMarketBody(BuildContext context, WidgetRef ref) {
    final state = widget.state;
    if (state.isLoading && state.marketplace.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }

    if (state.error != null && state.marketplace.isEmpty) {
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
              onPressed: () =>
                  ref.read(skillProvider.notifier).searchMarketplace(''),
              child: const Text('重试'),
            ),
          ],
        ),
      );
    }

    if (state.marketplace.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.storefront_outlined,
              size: 64,
              color: Theme.of(context).colorScheme.outlineVariant,
            ),
            const SizedBox(height: 16),
            Text(
              '暂无市场技能',
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
      itemCount: state.marketplace.length,
      itemBuilder: (context, index) {
        final pkg = state.marketplace[index];
        return _MarketplaceListTile(pkg: pkg);
      },
    );
  }

  Widget _buildPublicMarketBody(BuildContext context) {
    if (_publicLoading && _publicPackages.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }

    if (_publicError != null && _publicPackages.isEmpty) {
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
              _publicError!,
              style: Theme.of(context).textTheme.bodySmall,
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 16),
            FilledButton.tonal(
              onPressed: _browsePublicMarket,
              child: const Text('重试'),
            ),
          ],
        ),
      );
    }

    if (_publicPackages.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.public,
              size: 64,
              color: Theme.of(context).colorScheme.outlineVariant,
            ),
            const SizedBox(height: 16),
            Text(
              '暂无公开市场技能',
              style: Theme.of(context).textTheme.titleMedium?.copyWith(
                    color: Theme.of(context).colorScheme.outline,
                  ),
            ),
            const SizedBox(height: 8),
            FilledButton.tonal(
              onPressed: _browsePublicMarket,
              child: const Text('加载公开市场'),
            ),
          ],
        ),
      );
    }

    return ListView.builder(
      padding: const EdgeInsets.symmetric(vertical: 8),
      itemCount: _publicPackages.length,
      itemBuilder: (context, index) {
        final pkg = _publicPackages[index];
        return _PublicMarketListTile(pkg: pkg, onInstall: _installFromUrl);
      },
    );
  }
}

class _MarketplaceListTile extends ConsumerWidget {
  final MarketplacePackage pkg;

  const _MarketplaceListTile({required this.pkg});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    final isInstalled = pkg.installed == true;

    return ListTile(
      leading: CircleAvatar(
        backgroundColor: theme.colorScheme.tertiaryContainer,
        child: Icon(
          Icons.download,
          color: theme.colorScheme.onTertiaryContainer,
        ),
      ),
      title: Text(pkg.name),
      subtitle: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            pkg.description,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: theme.textTheme.bodySmall,
          ),
          if (pkg.author != null || pkg.downloads != null)
            Text(
              [
                if (pkg.author != null) '作者: ${pkg.author}',
                if (pkg.downloads != null) '下载: ${pkg.downloads}',
              ].join(' · '),
              style: theme.textTheme.labelSmall?.copyWith(
                color: theme.colorScheme.outline,
              ),
            ),
        ],
      ),
      trailing: isInstalled
          ? Chip(
              label: const Text('已安装'),
              visualDensity: VisualDensity.compact,
              backgroundColor: theme.colorScheme.primaryContainer,
              labelStyle: theme.textTheme.labelSmall?.copyWith(
                color: theme.colorScheme.onPrimaryContainer,
              ),
            )
          : FilledButton.tonal(
              onPressed: () async {
                final ok = await ref
                    .read(skillProvider.notifier)
                    .install(pkg.name);
                if (context.mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(
                    SnackBar(
                      content: Text(ok ? '安装成功' : '安装失败'),
                    ),
                  );
                }
              },
              child: const Text('安装'),
            ),
      isThreeLine: pkg.author != null || pkg.downloads != null,
    );
  }
}

/// 公开市场（GitHub 社区仓库）技能条目
class _PublicMarketListTile extends StatefulWidget {
  final Map<String, dynamic> pkg;
  final Future<void> Function(String source) onInstall;

  const _PublicMarketListTile({required this.pkg, required this.onInstall});

  @override
  State<_PublicMarketListTile> createState() => _PublicMarketListTileState();
}

class _PublicMarketListTileState extends State<_PublicMarketListTile> {
  bool _installing = false;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final pkg = widget.pkg;
    final name = pkg['name'] as String? ?? '';
    final description = pkg['description'] as String? ?? '';
    final author = pkg['author'] as String?;
    final downloads = pkg['downloads'];
    final source = (pkg['source'] as String?) ?? name;

    return ListTile(
      leading: CircleAvatar(
        backgroundColor: theme.colorScheme.tertiaryContainer,
        child: Icon(
          Icons.cloud_download_outlined,
          color: theme.colorScheme.onTertiaryContainer,
        ),
      ),
      title: Text(name.isEmpty ? '(未命名)' : name),
      subtitle: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            description.isEmpty ? '无描述' : description,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: theme.textTheme.bodySmall,
          ),
          if (author != null || downloads != null)
            Text(
              [
                if (author != null) '作者: $author',
                if (downloads != null) '下载: $downloads',
              ].join(' · '),
              style: theme.textTheme.labelSmall?.copyWith(
                color: theme.colorScheme.outline,
              ),
            ),
        ],
      ),
      trailing: FilledButton.tonal(
        onPressed: _installing
            ? null
            : () async {
                setState(() => _installing = true);
                try {
                  await widget.onInstall(source);
                } finally {
                  if (mounted) setState(() => _installing = false);
                }
              },
        child: _installing
            ? const SizedBox(
                width: 16,
                height: 16,
                child: CircularProgressIndicator(strokeWidth: 2),
              )
            : const Text('安装'),
      ),
      isThreeLine: author != null || downloads != null,
    );
  }
}
