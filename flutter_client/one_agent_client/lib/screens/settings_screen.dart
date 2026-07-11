import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/settings_provider.dart';
import '../providers/system_provider.dart';
import '../providers/update_provider.dart';

/// 设置页面
class SettingsScreen extends ConsumerWidget {
  const SettingsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final settingsState = ref.watch(settingsProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('设置'),
      ),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          _ServerUrlCard(),
          const SizedBox(height: 16),
          _ApiKeyCard(),
          const SizedBox(height: 16),
          _ActionCard(),
          const SizedBox(height: 16),
          _UpdateCard(),
          const SizedBox(height: 24),
          _ConnectionStatus(),
        ],
      ),
    );
  }
}

class _ServerUrlCard extends ConsumerStatefulWidget {
  @override
  ConsumerState<_ServerUrlCard> createState() => _ServerUrlCardState();
}

class _ServerUrlCardState extends ConsumerState<_ServerUrlCard> {
  late final TextEditingController _controller;

  @override
  void initState() {
    super.initState();
    _controller = TextEditingController();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final state = ref.read(settingsProvider);
    if (_controller.text != state.baseUrl) {
      _controller.text = state.baseUrl;
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '服务器地址',
              style: theme.textTheme.titleSmall?.copyWith(
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _controller,
              keyboardType: TextInputType.url,
              decoration: InputDecoration(
                hintText: 'http://192.168.1.100:18792',
                prefixIcon: const Icon(Icons.link),
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
              onSubmitted: (value) async {
                if (value.trim().isEmpty) return;
                await ref
                    .read(settingsProvider.notifier)
                    .setBaseUrl(value.trim());
                if (context.mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(content: Text('服务器地址已保存')),
                  );
                }
              },
            ),
          ],
        ),
      ),
    );
  }
}

class _ApiKeyCard extends ConsumerStatefulWidget {
  @override
  ConsumerState<_ApiKeyCard> createState() => _ApiKeyCardState();
}

class _ApiKeyCardState extends ConsumerState<_ApiKeyCard> {
  late final TextEditingController _controller;
  bool _obscure = true;

  @override
  void initState() {
    super.initState();
    _controller = TextEditingController();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final state = ref.read(settingsProvider);
    if (_controller.text != state.apiKey) {
      _controller.text = state.apiKey;
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'API Key',
              style: theme.textTheme.titleSmall?.copyWith(
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _controller,
              obscureText: _obscure,
              decoration: InputDecoration(
                hintText: '请输入 API Key',
                prefixIcon: const Icon(Icons.key),
                suffixIcon: IconButton(
                  icon: Icon(
                    _obscure ? Icons.visibility_off : Icons.visibility,
                  ),
                  onPressed: () => setState(() => _obscure = !_obscure),
                ),
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                ),
              ),
              onSubmitted: (value) async {
                await ref
                    .read(settingsProvider.notifier)
                    .setApiKey(value.trim());
                if (context.mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(content: Text('API Key 已保存')),
                  );
                }
              },
            ),
          ],
        ),
      ),
    );
  }
}

class _ActionCard extends ConsumerWidget {
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    final isLoading = ref.watch(settingsProvider).isLoading;

    return Card(
      child: Column(
        children: [
          ListTile(
            leading: Icon(Icons.network_check,
                color: theme.colorScheme.primary),
            title: const Text('测试连接'),
            subtitle: const Text('验证服务器连接状态'),
            trailing: isLoading
                ? const SizedBox(
                    width: 24,
                    height: 24,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.chevron_right),
            onTap: isLoading
                ? null
                : () async {
                    final ok = await ref
                        .read(settingsProvider.notifier)
                        .checkConnection();
                    if (context.mounted) {
                      ScaffoldMessenger.of(context).showSnackBar(
                        SnackBar(
                          content: Text(ok ? '连接成功' : '连接失败'),
                        ),
                      );
                    }
                  },
          ),
          const Divider(height: 1),
          ListTile(
            leading: Icon(Icons.cleaning_services,
                color: theme.colorScheme.secondary),
            title: const Text('清除缓存'),
            subtitle: const Text('清除本地缓存数据'),
            trailing: const Icon(Icons.chevron_right),
            onTap: () async {
              final confirmed = await showDialog<bool>(
                context: context,
                builder: (ctx) => AlertDialog(
                  title: const Text('清除缓存'),
                  content: const Text('确定要清除所有本地缓存数据吗？'),
                  actions: [
                    TextButton(
                      onPressed: () => Navigator.of(ctx).pop(false),
                      child: const Text('取消'),
                    ),
                    FilledButton(
                      onPressed: () => Navigator.of(ctx).pop(true),
                      child: const Text('清除'),
                    ),
                  ],
                ),
              );
              if (confirmed == true) {
                final ok = await ref
                    .read(systemProvider.notifier)
                    .clearCache();
                if (context.mounted) {
                  ScaffoldMessenger.of(context).showSnackBar(
                    SnackBar(
                      content: Text(ok ? '缓存已清除' : '清除失败'),
                    ),
                  );
                }
              }
            },
          ),
        ],
      ),
    );
  }
}

class _ConnectionStatus extends ConsumerWidget {
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final settings = ref.watch(settingsProvider);
    final theme = Theme.of(context);

    return Center(
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 10,
            height: 10,
            decoration: BoxDecoration(
              color: settings.isConnected ? Colors.green : Colors.red,
              shape: BoxShape.circle,
            ),
          ),
          const SizedBox(width: 8),
          Text(
            settings.isConnected ? '连接正常' : '未连接',
            style: theme.textTheme.bodySmall?.copyWith(
              color: theme.colorScheme.outline,
            ),
          ),
        ],
      ),
    );
  }
}

/// 应用更新卡片
class _UpdateCard extends ConsumerWidget {
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    final updateState = ref.watch(updateProvider);

    return Card(
      child: Column(
        children: [
          ListTile(
            leading: Icon(Icons.system_update,
                color: theme.colorScheme.tertiary),
            title: const Text('检查更新'),
            subtitle: Text(
              updateState.currentVersion != null
                  ? '当前版本 v${updateState.currentVersion}'
                  : '点击检查是否有新版本',
            ),
            trailing: updateState.isChecking
                ? const SizedBox(
                    width: 24,
                    height: 24,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.chevron_right),
            onTap: updateState.isChecking || updateState.isDownloading
                ? null
                : () async {
                    await ref.read(updateProvider.notifier).checkForUpdate();
                    final state = ref.read(updateProvider);
                    if (!context.mounted) return;
                    if (state.error != null) {
                      ScaffoldMessenger.of(context).showSnackBar(
                        SnackBar(content: Text(state.error!)),
                      );
                    } else if (state.hasUpdate) {
                      _showUpdateDialog(context, ref, state.latestRelease!);
                    } else {
                      ScaffoldMessenger.of(context).showSnackBar(
                        const SnackBar(content: Text('已是最新版本')),
                      );
                    }
                  },
          ),
          if (updateState.isDownloading) ...[
            const Divider(height: 1),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  LinearProgressIndicator(
                    value: updateState.downloadProgress > 0
                        ? updateState.downloadProgress
                        : null,
                  ),
                  const SizedBox(height: 8),
                  Text(
                    '下载中 ${(updateState.downloadProgress * 100).toStringAsFixed(0)}%',
                    style: theme.textTheme.bodySmall,
                  ),
                ],
              ),
            ),
          ],
          if (updateState.error != null && !updateState.isDownloading) ...[
            const Divider(height: 1),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              child: Row(
                children: [
                  Icon(Icons.error_outline,
                      size: 16, color: theme.colorScheme.error),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      updateState.error!,
                      style: theme.textTheme.bodySmall
                          ?.copyWith(color: theme.colorScheme.error),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }

  void _showUpdateDialog(
    BuildContext context,
    WidgetRef ref,
    dynamic release,
  ) {
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('发现新版本 ${release.tagName}'),
        content: SingleChildScrollView(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              if (release.body != null && release.body!.isNotEmpty)
                Text(release.body!),
              const SizedBox(height: 12),
              Text(
                '大小：${(release.apkSize / 1024 / 1024).toStringAsFixed(1)} MB',
                style: Theme.of(context).textTheme.bodySmall,
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('稍后再说'),
          ),
          FilledButton(
            onPressed: () {
              Navigator.of(ctx).pop();
              ref.read(updateProvider.notifier).downloadAndInstall();
            },
            child: const Text('立即更新'),
          ),
        ],
      ),
    );
  }
}
