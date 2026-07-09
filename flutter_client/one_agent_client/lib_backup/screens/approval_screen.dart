import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart';

import '../providers/approval_provider.dart';
import '../models/approval.dart';

/// 审批页面
class ApprovalScreen extends ConsumerWidget {
  const ApprovalScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final approvalState = ref.watch(approvalProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('审批中心'),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: '刷新',
            onPressed: () => ref.read(approvalProvider.notifier).load(),
          ),
        ],
      ),
      body: _buildBody(context, ref, approvalState),
    );
  }

  Widget _buildBody(
    BuildContext context,
    WidgetRef ref,
    ApprovalState state,
  ) {
    if (state.isLoading && state.pending.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }

    if (state.error != null && state.pending.isEmpty) {
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
              onPressed: () => ref.read(approvalProvider.notifier).load(),
              child: const Text('重试'),
            ),
          ],
        ),
      );
    }

    if (state.pending.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.check_circle_outline,
              size: 64,
              color: Theme.of(context).colorScheme.outlineVariant,
            ),
            const SizedBox(height: 16),
            Text(
              '暂无待审批请求',
              style: Theme.of(context).textTheme.titleMedium?.copyWith(
                    color: Theme.of(context).colorScheme.outline,
                  ),
            ),
            const SizedBox(height: 8),
            Text(
              '所有请求已处理完毕',
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                    color: Theme.of(context).colorScheme.outlineVariant,
                  ),
            ),
          ],
        ),
      );
    }

    return ListView.builder(
      padding: const EdgeInsets.symmetric(vertical: 8),
      itemCount: state.pending.length,
      itemBuilder: (context, index) {
        final request = state.pending[index];
        return _ApprovalCard(request: request);
      },
    );
  }
}

class _ApprovalCard extends ConsumerWidget {
  final ApprovalRequest request;

  const _ApprovalCard({required this.request});

  Color _riskColor(BuildContext context) {
    switch (request.riskLevel) {
      case 'high':
        return Theme.of(context).colorScheme.error;
      case 'medium':
        return Theme.of(context).colorScheme.errorContainer;
      default:
        return Theme.of(context).colorScheme.surfaceContainerHighest;
    }
  }

  Color _riskTextColor(BuildContext context) {
    switch (request.riskLevel) {
      case 'high':
        return Theme.of(context).colorScheme.onError;
      case 'medium':
        return Theme.of(context).colorScheme.onErrorContainer;
      default:
        return Theme.of(context).colorScheme.onSurfaceVariant;
    }
  }

  String _riskLabel() {
    switch (request.riskLevel) {
      case 'high':
        return '高风险';
      case 'medium':
        return '中风险';
      default:
        return '低风险';
    }
  }

  String _formatTime(DateTime? dt) {
    if (dt == null) return '';
    return DateFormat('MM-dd HH:mm').format(dt);
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    final isHighRisk = request.isHighRisk;

    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      color: isHighRisk
          ? theme.colorScheme.errorContainer.withOpacity(0.3)
          : null,
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    request.operation,
                    style: theme.textTheme.titleMedium?.copyWith(
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 10,
                    vertical: 4,
                  ),
                  decoration: BoxDecoration(
                    color: _riskColor(context),
                    borderRadius: BorderRadius.circular(12),
                  ),
                  child: Text(
                    _riskLabel(),
                    style: theme.textTheme.labelSmall?.copyWith(
                      color: _riskTextColor(context),
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
              ],
            ),
            if (request.details != null && request.details!.isNotEmpty) ...[
              const SizedBox(height: 8),
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  color: theme.colorScheme.surfaceContainerHighest,
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Text(
                  request.details!,
                  style: theme.textTheme.bodySmall?.copyWith(
                    color: theme.colorScheme.onSurfaceVariant,
                  ),
                ),
              ),
            ],
            const SizedBox(height: 10),
            Row(
              children: [
                Text(
                  _formatTime(request.createdAt),
                  style: theme.textTheme.labelSmall?.copyWith(
                    color: theme.colorScheme.outline,
                  ),
                ),
                const Spacer(),
                OutlinedButton.icon(
                  onPressed: () async {
                    final ok = await ref
                        .read(approvalProvider.notifier)
                        .deny(request.id);
                    if (context.mounted) {
                      ScaffoldMessenger.of(context).showSnackBar(
                        SnackBar(
                          content: Text(ok ? '已拒绝' : '操作失败'),
                        ),
                      );
                    }
                  },
                  icon: const Icon(Icons.close, size: 18),
                  label: const Text('拒绝'),
                  style: OutlinedButton.styleFrom(
                    foregroundColor: theme.colorScheme.error,
                  ),
                ),
                const SizedBox(width: 8),
                FilledButton.icon(
                  onPressed: () async {
                    final ok = await ref
                        .read(approvalProvider.notifier)
                        .approve(request.id);
                    if (context.mounted) {
                      ScaffoldMessenger.of(context).showSnackBar(
                        SnackBar(
                          content: Text(ok ? '已通过' : '操作失败'),
                        ),
                      );
                    }
                  },
                  icon: const Icon(Icons.check, size: 18),
                  label: const Text('通过'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
