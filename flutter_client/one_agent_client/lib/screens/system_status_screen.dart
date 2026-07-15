import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart';

import '../api/session_api.dart';
import '../api/system_api.dart';
import '../models/session.dart';
import '../providers/system_provider.dart';
import '../providers/settings_provider.dart';
import 'log_viewer_screen.dart';
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
            icon: const Icon(Icons.article_outlined),
            tooltip: '查看日志',
            onPressed: () {
              Navigator.of(context).push(
                MaterialPageRoute(builder: (_) => const LogViewerScreen()),
              );
            },
          ),
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
        _StatsGrid(
          stats: state.stats,
          onTapSessions: () => _showSessionsSheet(context),
        ),
        const SizedBox(height: 16),
        _LlmStatsCard(llmStats: state.stats?.llmStats),
        const SizedBox(height: 16),
        _RouterStatsCard(),
        const SizedBox(height: 16),
        _CostCard(costs: state.costs),
        const SizedBox(height: 16),
        _HealthCard(health: state.health),
      ],
    );
  }

  /// 弹出底部抽屉，展示会话列表；点击会话进入消息列表
  void _showSessionsSheet(BuildContext context) {
    showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (_) => const _SessionsSheet(),
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
  /// 点击"会话数"/"消息数"卡片时触发（弹出会话列表）
  final VoidCallback? onTapSessions;

  const _StatsGrid({this.stats, this.onTapSessions});

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
        onTap: null,
      ),
      _StatItem(
        label: '会话数',
        value: '${_sessionCount()}',
        icon: Icons.chat_bubble,
        onTap: onTapSessions,
      ),
      _StatItem(
        label: '消息数',
        value: '${_messageCount()}',
        icon: Icons.message,
        onTap: onTapSessions,
      ),
      _StatItem(
        label: '运行时间',
        value: _formatUptime(stats?.uptimeSeconds),
        icon: Icons.timer,
        onTap: null,
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
  /// 非空时卡片可点击
  final VoidCallback? onTap;

  _StatItem({
    required this.label,
    required this.value,
    required this.icon,
    this.onTap,
  });
}

class _StatCard extends StatelessWidget {
  final _StatItem item;

  const _StatCard({required this.item});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final clickable = item.onTap != null;

    final card = Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Row(
              children: [
                Icon(item.icon, size: 22, color: theme.colorScheme.primary),
                const Spacer(),
                if (clickable)
                  Icon(
                    Icons.chevron_right,
                    size: 18,
                    color: theme.colorScheme.outline,
                  ),
              ],
            ),
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

    if (!clickable) return card;
    return InkWell(
      borderRadius: BorderRadius.circular(16),
      onTap: item.onTap,
      child: card,
    );
  }
}

// ═══════════════════════════════════════════════════════════════════
// LLM 调用统计卡片
// ═══════════════════════════════════════════════════════════════════

class _LlmStatsCard extends StatelessWidget {
  final Map<String, dynamic>? llmStats;

  const _LlmStatsCard({this.llmStats});

  int _asInt(dynamic v) {
    if (v is int) return v;
    if (v is double) return v.toInt();
    if (v is num) return v.toInt();
    return 0;
  }

  double _asDouble(dynamic v) {
    if (v is num) return v.toDouble();
    return 0;
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final totalCalls = _asInt(llmStats?['total_calls'] ??
        llmStats?['calls'] ??
        llmStats?['request_count']);
    final cacheHits = _asInt(llmStats?['cache_hits']);
    final cacheHitRate = _asDouble(
        llmStats?['cache_hit_rate'] ?? llmStats?['hit_rate']);
    final failures = _asInt(llmStats?['failures'] ??
        llmStats?['errors'] ??
        llmStats?['failed_calls']);

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(Icons.psychology,
                    size: 18, color: theme.colorScheme.primary),
                const SizedBox(width: 8),
                Text(
                  'LLM 调用统计',
                  style: theme.textTheme.titleSmall?.copyWith(
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: _Metric(
                    label: '调用次数',
                    value: '$totalCalls',
                    icon: Icons.call_made,
                    color: theme.colorScheme.primary,
                  ),
                ),
                Expanded(
                  child: _Metric(
                    label: '缓存命中',
                    value: '$cacheHits',
                    icon: Icons.cached,
                    color: Colors.green,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 10),
            Row(
              children: [
                Expanded(
                  child: _Metric(
                    label: '缓存命中率',
                    value: cacheHitRate > 0
                        ? '${(cacheHitRate * 100).toStringAsFixed(1)}%'
                        : '-',
                    icon: Icons.percent,
                    color: Colors.teal,
                  ),
                ),
                Expanded(
                  child: _Metric(
                    label: '失败次数',
                    value: '$failures',
                    icon: Icons.error_outline,
                    color: failures > 0
                        ? theme.colorScheme.error
                        : theme.colorScheme.outline,
                  ),
                ),
              ],
            ),
            if (llmStats == null)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  '暂无 LLM 统计数据',
                  style: theme.textTheme.bodySmall?.copyWith(
                    color: theme.colorScheme.outline,
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _Metric extends StatelessWidget {
  final String label;
  final String value;
  final IconData icon;
  final Color color;

  const _Metric({
    required this.label,
    required this.value,
    required this.icon,
    required this.color,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Row(
      children: [
        Icon(icon, size: 18, color: color),
        const SizedBox(width: 8),
        Flexible(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                value,
                style: theme.textTheme.titleMedium?.copyWith(
                  fontWeight: FontWeight.w600,
                  fontFamily: 'monospace',
                ),
                overflow: TextOverflow.ellipsis,
              ),
              Text(
                label,
                style: theme.textTheme.labelSmall?.copyWith(
                  color: theme.colorScheme.outline,
                ),
              ),
            ],
          ),
        ),
      ],
    );
  }
}

// ═══════════════════════════════════════════════════════════════════
// 路由统计卡片 — 通过 SystemApi.getModels() 获取 4 层路由各自被选中的次数
// ═══════════════════════════════════════════════════════════════════

class _RouterStatsCard extends StatefulWidget {
  @override
  State<_RouterStatsCard> createState() => _RouterStatsCardState();
}

class _RouterStatsCardState extends State<_RouterStatsCard> {
  Map<String, dynamic>? _tiers;
  bool _loading = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final result = await SystemApi.getModels();
      if (!mounted) return;
      setState(() {
        _tiers = result?['tiers'] as Map<String, dynamic>?;
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e.toString();
        _loading = false;
      });
    }
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
            Row(
              children: [
                Icon(Icons.alt_route,
                    size: 18, color: theme.colorScheme.primary),
                const SizedBox(width: 8),
                Text(
                  '路由统计',
                  style: theme.textTheme.titleSmall?.copyWith(
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const Spacer(),
                if (_loading)
                  SizedBox(
                    width: 14,
                    height: 14,
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                      color: theme.colorScheme.outline,
                    ),
                  )
                else
                  IconButton(
                    icon: const Icon(Icons.refresh, size: 18),
                    padding: EdgeInsets.zero,
                    constraints: const BoxConstraints(),
                    tooltip: '刷新',
                    onPressed: _load,
                  ),
              ],
            ),
            const SizedBox(height: 12),
            _buildBody(theme),
          ],
        ),
      ),
    );
  }

  Widget _buildBody(ThemeData theme) {
    if (_loading && _tiers == null) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 8),
        child: Center(child: Text('加载中...')),
      );
    }
    if (_tiers == null || _tiers!.isEmpty) {
      return Text(
        _error ?? '暂无路由统计数据',
        style: theme.textTheme.bodySmall?.copyWith(
          color: theme.colorScheme.outline,
        ),
      );
    }

    // 4 层路由的颜色和标签
    const tierColors = {
      'trivial': Colors.green,
      'simple': Colors.blue,
      'complex': Colors.orange,
      'expert': Colors.red,
    };
    const tierLabels = {
      'trivial': '极简',
      'simple': '简单',
      'complex': '复杂',
      'expert': '专家',
    };

    final tierNames = ['trivial', 'simple', 'complex', 'expert'];
    final totalPicked = tierNames.fold<int>(0, (sum, name) {
      final tier = _tiers![name] as Map<String, dynamic>?;
      final picked = (tier?['stats'] as Map?)?['picked'] ??
          tier?['picked'] ??
          0;
      return sum + ((picked is num) ? picked.toInt() : 0);
    });

    return Column(
      children: tierNames.map((name) {
        final tier = _tiers![name] as Map<String, dynamic>?;
        final picked = tier?['stats'] != null
            ? (tier!['stats'] as Map)['picked']
            : tier?['picked'];
        final count = (picked is num) ? picked.toInt() : 0;
        final color = tierColors[name] ?? theme.colorScheme.primary;
        final label = tierLabels[name] ?? name;
        final percent = totalPicked > 0 ? count / totalPicked : 0.0;

        return Padding(
          padding: const EdgeInsets.symmetric(vertical: 4),
          child: Row(
            children: [
              Container(
                width: 8,
                height: 8,
                decoration: BoxDecoration(
                  color: color,
                  shape: BoxShape.circle,
                ),
              ),
              const SizedBox(width: 10),
              SizedBox(
                width: 48,
                child: Text(label, style: theme.textTheme.bodyMedium),
              ),
              Expanded(
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(4),
                  child: LinearProgressIndicator(
                    value: percent,
                    minHeight: 6,
                    backgroundColor:
                        color.withOpacity(0.15),
                    color: color,
                  ),
                ),
              ),
              const SizedBox(width: 10),
              SizedBox(
                width: 56,
                child: Text(
                  '$count 次',
                  textAlign: TextAlign.end,
                  style: theme.textTheme.bodySmall?.copyWith(
                    fontFamily: 'monospace',
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
            ],
          ),
        );
      }).toList(),
    );
  }
}

// ═══════════════════════════════════════════════════════════════════
// 成本统计卡片
// ═══════════════════════════════════════════════════════════════════

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

// ═══════════════════════════════════════════════════════════════════
// 会话列表底部抽屉 — 点击会话查看其消息列表
// ═══════════════════════════════════════════════════════════════════

class _SessionsSheet extends ConsumerStatefulWidget {
  const _SessionsSheet();

  @override
  ConsumerState<_SessionsSheet> createState() => _SessionsSheetState();
}

class _SessionsSheetState extends ConsumerState<_SessionsSheet> {
  List<Session> _sessions = [];
  bool _isLoading = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _load());
  }

  Future<void> _load() async {
    setState(() {
      _isLoading = true;
      _error = null;
    });
    try {
      final sessions = await SessionApi.listSessions(limit: 100);
      if (!mounted) return;
      setState(() {
        _sessions = sessions;
        _isLoading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e.toString();
        _isLoading = false;
      });
    }
  }

  String _formatTime(DateTime? dt) {
    if (dt == null) return '';
    return DateFormat('MM-dd HH:mm').format(dt);
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return DraggableScrollableSheet(
      initialChildSize: 0.7,
      minChildSize: 0.4,
      maxChildSize: 0.95,
      expand: false,
      builder: (context, scrollController) {
        return Column(
          children: [
            // 顶部把手 + 标题栏
            Container(
              padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
              child: Row(
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Center(
                          child: Container(
                            width: 36,
                            height: 4,
                            margin: const EdgeInsets.only(bottom: 8),
                            decoration: BoxDecoration(
                              color: theme.colorScheme.outlineVariant,
                              borderRadius: BorderRadius.circular(2),
                            ),
                          ),
                        ),
                        Text(
                          '会话列表 (${_sessions.length})',
                          style: theme.textTheme.titleMedium?.copyWith(
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ],
                    ),
                  ),
                  IconButton(
                    icon: const Icon(Icons.refresh),
                    tooltip: '刷新',
                    onPressed: _isLoading ? null : _load,
                  ),
                ],
              ),
            ),
            const Divider(height: 1),
            Expanded(child: _buildBody(theme, scrollController)),
          ],
        );
      },
    );
  }

  Widget _buildBody(ThemeData theme, ScrollController scrollController) {
    if (_isLoading && _sessions.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null && _sessions.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.error_outline,
                size: 48, color: theme.colorScheme.error),
            const SizedBox(height: 12),
            const Text('加载失败'),
            const SizedBox(height: 4),
            Text(_error!, style: theme.textTheme.bodySmall),
            const SizedBox(height: 16),
            FilledButton.tonal(
              onPressed: _load,
              child: const Text('重试'),
            ),
          ],
        ),
      );
    }
    if (_sessions.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.chat_bubble_outline,
                size: 64, color: theme.colorScheme.outlineVariant),
            const SizedBox(height: 16),
            Text(
              '暂无会话',
              style: theme.textTheme.titleMedium?.copyWith(
                color: theme.colorScheme.outline,
              ),
            ),
          ],
        ),
      );
    }
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView.separated(
        controller: scrollController,
        padding: const EdgeInsets.symmetric(vertical: 8),
        itemCount: _sessions.length,
        separatorBuilder: (_, __) => const Divider(height: 1, indent: 56),
        itemBuilder: (context, index) {
          final session = _sessions[index];
          return ListTile(
            leading: CircleAvatar(
              backgroundColor: theme.colorScheme.primaryContainer,
              child: Icon(
                Icons.chat_bubble_outline,
                color: theme.colorScheme.onPrimaryContainer,
                size: 18,
              ),
            ),
            title: Text(
              session.title ?? '未命名会话',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
            subtitle: Text(
              '${_formatTime(session.createdAt)} · ${session.messageCount ?? 0} 条消息',
              style: theme.textTheme.bodySmall,
            ),
            trailing: const Icon(Icons.chevron_right, size: 20),
            onTap: () => _openMessages(context, session),
          );
        },
      ),
    );
  }

  /// 打开会话消息列表（以全屏对话框形式展示）
  void _openMessages(BuildContext context, Session session) {
    Navigator.of(context).push(
      MaterialPageRoute(
        fullscreenDialog: true,
        builder: (_) => _SessionMessagesScreen(session: session),
      ),
    );
  }
}

/// 会话消息列表页面 — 调用 SessionApi.getSessionMessages 展示
class _SessionMessagesScreen extends StatefulWidget {
  final Session session;

  const _SessionMessagesScreen({required this.session});

  @override
  State<_SessionMessagesScreen> createState() =>
      _SessionMessagesScreenState();
}

class _SessionMessagesScreenState extends State<_SessionMessagesScreen> {
  List<Map<String, dynamic>> _messages = [];
  bool _isLoading = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _load());
  }

  Future<void> _load() async {
    setState(() {
      _isLoading = true;
      _error = null;
    });
    try {
      final result =
          await SessionApi.getSessionMessages(widget.session.id, limit: 200);
      if (!mounted) return;
      if (result == null) {
        setState(() {
          _isLoading = false;
          _error = '加载消息失败';
        });
        return;
      }
      final msgs = (result['messages'] as List<dynamic>? ?? [])
          .map((e) => e as Map<String, dynamic>)
          .toList();
      setState(() {
        _messages = msgs;
        _isLoading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e.toString();
        _isLoading = false;
      });
    }
  }

  String _msgRole(dynamic role) {
    if (role == null) return 'system';
    final s = role.toString();
    switch (s.toLowerCase()) {
      case 'user':
        return '用户';
      case 'assistant':
        return '助手';
      case 'system':
        return '系统';
      case 'tool':
        return '工具';
      case 'thinking':
        return '思考';
      default:
        return s;
    }
  }

  String _msgContent(Map<String, dynamic> m) {
    return (m['content'] ??
            m['text'] ??
            m['reply'] ??
            '')
        .toString();
  }

  String _msgTime(Map<String, dynamic> m) {
    final ts = m['timestamp'] ?? m['created_at'] ?? m['time'];
    if (ts == null) return '';
    DateTime? dt;
    if (ts is num) {
      dt = DateTime.fromMillisecondsSinceEpoch(
        ts > 1e12 ? ts.toInt() : (ts * 1000).toInt(),
      );
    } else {
      dt = DateTime.tryParse(ts.toString());
    }
    if (dt == null) return '';
    return DateFormat('MM-dd HH:mm:ss').format(dt);
  }

  Color _roleColor(String role, ThemeData theme) {
    switch (role) {
      case '用户':
        return theme.colorScheme.primary;
      case '助手':
        return Colors.teal;
      case '系统':
        return theme.colorScheme.outline;
      case '工具':
        return Colors.deepPurple;
      case '思考':
        return Colors.orange;
      default:
        return theme.colorScheme.outline;
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(
        title: Text(
          widget.session.title ?? '会话消息',
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: '刷新',
            onPressed: _isLoading ? null : _load,
          ),
        ],
      ),
      body: _buildBody(theme),
    );
  }

  Widget _buildBody(ThemeData theme) {
    if (_isLoading && _messages.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null && _messages.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.error_outline,
                size: 48, color: theme.colorScheme.error),
            const SizedBox(height: 12),
            const Text('加载失败'),
            const SizedBox(height: 4),
            Text(_error!, style: theme.textTheme.bodySmall),
            const SizedBox(height: 16),
            FilledButton.tonal(
              onPressed: _load,
              child: const Text('重试'),
            ),
          ],
        ),
      );
    }
    if (_messages.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.message_outlined,
                size: 64, color: theme.colorScheme.outlineVariant),
            const SizedBox(height: 16),
            Text(
              '暂无消息',
              style: theme.textTheme.titleMedium?.copyWith(
                color: theme.colorScheme.outline,
              ),
            ),
          ],
        ),
      );
    }
    return SelectionArea(
      child: ListView.separated(
        padding: const EdgeInsets.symmetric(vertical: 8),
        itemCount: _messages.length,
        separatorBuilder: (_, __) => const Divider(height: 1, indent: 12),
        itemBuilder: (context, index) {
          final m = _messages[index];
          final role = _msgRole(m['role']);
          final content = _msgContent(m);
          final time = _msgTime(m);
          final color = _roleColor(role, theme);
          return Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 6, vertical: 2),
                      decoration: BoxDecoration(
                        color: color.withOpacity(0.15),
                        borderRadius: BorderRadius.circular(4),
                      ),
                      child: Text(
                        role,
                        style: theme.textTheme.labelSmall?.copyWith(
                          color: color,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                    const SizedBox(width: 8),
                    if (time.isNotEmpty)
                      Text(
                        time,
                        style: theme.textTheme.labelSmall?.copyWith(
                          fontFamily: 'monospace',
                          color: theme.colorScheme.outline,
                        ),
                      ),
                  ],
                ),
                const SizedBox(height: 6),
                SelectableText(
                  content,
                  style: theme.textTheme.bodyMedium?.copyWith(
                    height: 1.4,
                  ),
                ),
              ],
            ),
          );
        },
      ),
    );
  }
}
