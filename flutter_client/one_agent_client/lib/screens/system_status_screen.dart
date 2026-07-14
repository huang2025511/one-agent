import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/system_provider.dart';
import '../providers/settings_provider.dart';
import 'settings_screen.dart';

/// 系统状态页面
class SystemStatusScreen extends ConsumerWidget {
  const SystemStatusScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final systemState = ref.watch(systemProvider);
    final settingsState = ref.watch(settingsProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('系统状态'),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: '刷新',
            onPressed: () => ref.read(systemProvider.notifier).loadAll(),
          ),
          IconButton(
            icon: const Icon(Icons.settings_outlined),
            tooltip: '设置',
            onPressed: () {
              Navigator.of(context).push(
                MaterialPageRoute(builder: (_) => const SettingsScreen()),
              );
            },
          ),
        ],
      ),
      body: _buildBody(context, ref, systemState, settingsState),
    );
  }

  Widget _buildBody(
    BuildContext context,
    WidgetRef ref,
    SystemState state,
    SettingsState settings,
  ) {
    if (state.isLoading && state.stats == null) {
      return const Center(child: CircularProgressIndicator());
    }

    if (state.error != null && state.stats == null) {
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
              onPressed: () => ref.read(systemProvider.notifier).loadAll(),
              child: const Text('重试'),
            ),
          ],
        ),
      );
    }

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        _ConnectionCard(isConnected: settings.isConnected),
        const SizedBox(height: 16),
        _StatsGrid(stats: state.stats),
        const SizedBox(height: 16),
        _CostCard(costs: state.costs),
        const SizedBox(height: 16),
        _HealthCard(health: state.health),
      ],
    );
  }
}

class _ConnectionCard extends StatelessWidget {
  final bool isConnected;

  const _ConnectionCard({required this.isConnected});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            Container(
              width: 48,
              height: 48,
              decoration: BoxDecoration(
                color: isConnected
                    ? theme.colorScheme.primaryContainer
                    : theme.colorScheme.errorContainer,
                shape: BoxShape.circle,
              ),
              child: Icon(
                isConnected ? Icons.wifi : Icons.wifi_off,
                color: isConnected
                    ? theme.colorScheme.onPrimaryContainer
                    : theme.colorScheme.onErrorContainer,
              ),
            ),
            const SizedBox(width: 16),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    isConnected ? '已连接' : '未连接',
                    style: theme.textTheme.titleMedium?.copyWith(
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    isConnected
                        ? '服务器连接正常'
                        : '无法连接到服务器，请检查设置',
                    style: theme.textTheme.bodySmall?.copyWith(
                      color: theme.colorScheme.outline,
                    ),
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

class _StatsGrid extends StatelessWidget {
  final dynamic stats;

  const _StatsGrid({this.stats});

  int _getInt(dynamic value) {
    if (value == null) return 0;
    if (value is int) return value;
    if (value is double) return value.toInt();
    if (value is Map) {
      return value.length;
    }
    return 0;
  }

  int _sessionCount() {
    if (stats == null) return 0;
    final s = stats.sessions;
    if (s is Map) return s.length;
    if (s is int) return s;
    return 0;
  }

  int _messageCount() {
    if (stats == null) return 0;
    final m = stats.messages;
    if (m is Map) return m.length;
    if (m is int) return m;
    return 0;
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final items = [
      _StatItem(
        label: '技能数',
        value: '${stats?.skillsCount ?? 0}',
        icon: Icons.extension,
      ),
      _StatItem(
        label: '会话数',
        value: '${_sessionCount()}',
        icon: Icons.chat_bubble,
      ),
      _StatItem(
        label: '消息数',
        value: '${_messageCount()}',
        icon: Icons.message,
      ),
      _StatItem(
        label: '运行时间',
        value: _formatUptime(stats?.uptimeSeconds),
        icon: Icons.timer,
      ),
    ];

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          '统计信息',
          style: theme.textTheme.titleSmall?.copyWith(
            fontWeight: FontWeight.w600,
          ),
        ),
        const SizedBox(height: 10),
        GridView.count(
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          crossAxisCount: 2,
          mainAxisSpacing: 10,
          crossAxisSpacing: 10,
          childAspectRatio: 1.6,
          children: items.map((item) => _StatCard(item: item)).toList(),
        ),
      ],
    );
  }

  String _formatUptime(int? seconds) {
    if (seconds == null || seconds <= 0) return '-';
    final d = Duration(seconds: seconds);
    if (d.inDays > 0) return '${d.inDays}d ${d.inHours % 24}h';
    if (d.inHours > 0) return '${d.inHours}h ${d.inMinutes % 60}m';
    return '${d.inMinutes}m';
  }
}

class _StatItem {
  final String label;
  final String value;
  final IconData icon;

  _StatItem({required this.label, required this.value, required this.icon});
}

class _StatCard extends StatelessWidget {
  final _StatItem item;

  const _StatCard({required this.item});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(item.icon, size: 24, color: theme.colorScheme.primary),
            const SizedBox(height: 8),
            Text(
              item.value,
              style: theme.textTheme.titleLarge?.copyWith(
                fontWeight: FontWeight.bold,
              ),
            ),
            const SizedBox(height: 2),
            Text(
              item.label,
              style: theme.textTheme.labelSmall?.copyWith(
                color: theme.colorScheme.outline,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _CostCard extends StatelessWidget {
  final dynamic costs;

  const _CostCard({this.costs});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final totalTokens = costs?.totalTokens ?? 0;
    final totalCost = costs?.totalCost ?? 0.0;

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '成本统计',
              style: theme.textTheme.titleSmall?.copyWith(
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: _CostMetric(
                    label: '总 Token',
                    value: '$totalTokens',
                    icon: Icons.token,
                  ),
                ),
                Expanded(
                  child: _CostMetric(
                    label: '总成本',
                    value: '\$${totalCost.toStringAsFixed(4)}',
                    icon: Icons.attach_money,
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

class _CostMetric extends StatelessWidget {
  final String label;
  final String value;
  final IconData icon;

  const _CostMetric({
    required this.label,
    required this.value,
    required this.icon,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Row(
      children: [
        Icon(icon, size: 20, color: theme.colorScheme.secondary),
        const SizedBox(width: 8),
        Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              value,
              style: theme.textTheme.titleMedium?.copyWith(
                fontWeight: FontWeight.w600,
              ),
            ),
            Text(
              label,
              style: theme.textTheme.labelSmall?.copyWith(
                color: theme.colorScheme.outline,
              ),
            ),
          ],
        ),
      ],
    );
  }
}

class _HealthCard extends StatelessWidget {
  final dynamic health;

  const _HealthCard({this.health});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final status = health?.status ?? 'unknown';
    final version = health?.version ?? '-';

    Color statusColor;
    switch (status.toString().toLowerCase()) {
      case 'healthy':
      case 'ok':
        statusColor = Colors.green;
        break;
      case 'degraded':
        statusColor = Colors.orange;
        break;
      case 'unhealthy':
      case 'error':
        statusColor = theme.colorScheme.error;
        break;
      default:
        statusColor = theme.colorScheme.outline;
    }

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              '健康检查',
              style: theme.textTheme.titleSmall?.copyWith(
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                Icon(Icons.favorite, color: statusColor, size: 20),
                const SizedBox(width: 8),
                Text(
                  '状态: $status',
                  style: theme.textTheme.bodyMedium,
                ),
              ],
            ),
            const SizedBox(height: 8),
            Row(
              children: [
                Icon(Icons.info_outline,
                    color: theme.colorScheme.outline, size: 20),
                const SizedBox(width: 8),
                Text(
                  '版本: $version',
                  style: theme.textTheme.bodyMedium,
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
