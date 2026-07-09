import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/skill_provider.dart';
import '../models/skill.dart';

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
    if (_tabController.index == 1) {
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
                ref.read(skillProvider.notifier).searchMarketplace('');
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

class _SkillListTile extends StatelessWidget {
  final Skill skill;

  const _SkillListTile({required this.skill});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return ListTile(
      leading: CircleAvatar(
        backgroundColor: theme.colorScheme.secondaryContainer,
        child: Icon(
          Icons.extension,
          color: theme.colorScheme.onSecondaryContainer,
        ),
      ),
      title: Text(skill.title),
      subtitle: Text(
        skill.description ?? '无描述',
        maxLines: 2,
        overflow: TextOverflow.ellipsis,
        style: theme.textTheme.bodySmall,
      ),
      trailing: Chip(
        label: Text(skill.version ?? 'v1.0'),
        visualDensity: VisualDensity.compact,
      ),
    );
  }
}

class _MarketplaceTab extends ConsumerWidget {
  final SkillState state;
  final TextEditingController searchController;

  const _MarketplaceTab({
    required this.state,
    required this.searchController,
  });

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);

    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.all(12),
          child: SearchBar(
            controller: searchController,
            hintText: '搜索技能市场...',
            leading: const Icon(Icons.search),
            trailing: [
              if (searchController.text.isNotEmpty)
                IconButton(
                  icon: const Icon(Icons.clear),
                  onPressed: () {
                    searchController.clear();
                    ref.read(skillProvider.notifier).searchMarketplace('');
                  },
                ),
            ],
            onSubmitted: (value) {
              ref.read(skillProvider.notifier).searchMarketplace(value.trim());
            },
            backgroundColor: WidgetStatePropertyAll(
              theme.colorScheme.surfaceContainerHighest,
            ),
            elevation: const WidgetStatePropertyAll(0),
          ),
        ),
        Expanded(
          child: _buildMarketplaceBody(context, ref),
        ),
      ],
    );
  }

  Widget _buildMarketplaceBody(BuildContext context, WidgetRef ref) {
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
