import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/approval_provider.dart';
import '../providers/update_provider.dart';
import 'chat_screen.dart';
import 'memory_screen.dart';
import 'skill_screen.dart';
import 'approval_screen.dart';
import 'system_status_screen.dart';

/// 底部导航主页面
class MainScreen extends ConsumerStatefulWidget {
  const MainScreen({super.key});

  @override
  ConsumerState<MainScreen> createState() => _MainScreenState();
}

class _MainScreenState extends ConsumerState<MainScreen> {
  int _currentIndex = 0;
  bool _updateCheckDone = false;

  final _pages = const [
    ChatScreen(),
    MemoryScreen(),
    SkillScreen(),
    ApprovalScreen(),
    SystemStatusScreen(),
  ];

  final _labels = ['聊天', '记忆', '技能', '审批', '状态'];
  final _icons = [
    Icons.chat_bubble_outline,
    Icons.memory_outlined,
    Icons.extension_outlined,
    Icons.gavel_outlined,
    Icons.monitor_heart_outlined,
  ];
  final _activeIcons = [
    Icons.chat_bubble,
    Icons.memory,
    Icons.extension,
    Icons.gavel,
    Icons.monitor_heart,
  ];

  @override
  void initState() {
    super.initState();
    // 延迟启动审批轮询和更新检查，等设置加载完成
    WidgetsBinding.instance.addPostFrameCallback((_) {
      ref.read(approvalProvider.notifier).startPolling();
      _checkUpdateOnStartup();
    });
  }

  /// 启动时自动检查更新（延迟 3 秒，等网络连接建立）
  Future<void> _checkUpdateOnStartup() async {
    if (_updateCheckDone) return;
    // 修复：先延迟，再做幂等标记 — 避免 widget 在 3 秒内 dispose 导致整个 turn 被永久跳过
    // 延迟确保网络和服务已就绪
    await Future.delayed(const Duration(seconds: 3));
    if (!mounted) {
      // widget 已 dispose，下次启动允许重新检查
      return;
    }
    _updateCheckDone = true;
    await ref.read(updateProvider.notifier).checkForUpdate();
    if (!mounted) return;
    final updateState = ref.read(updateProvider);
    if (updateState.error != null) {
      // 静默失败 — 用户可在"设置 → 检查更新"手动重试
      debugPrint('startup update check failed: ${updateState.error}');
      return;
    }
    if (updateState.hasUpdate && updateState.latestRelease != null) {
      _showUpdateNotification(updateState.latestRelease!.tagName);
    }
  }

  void _showUpdateNotification(String tagName) {
    if (!mounted) return;
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('发现新版本'),
        content: Text('新版本 $tagName 可用，建议更新。'),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('稍后'),
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

  @override
  void dispose() {
    // 在 super.dispose() 之前停止轮询，ref 仍有效
    ref.read(approvalProvider.notifier).stopPolling();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final approvalState = ref.watch(approvalProvider);
    final pendingCount = approvalState.pending.where((a) => a.isPending).length;

    return Scaffold(
      body: IndexedStack(
        index: _currentIndex,
        children: _pages,
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _currentIndex,
        onDestinationSelected: (index) => setState(() => _currentIndex = index),
        destinations: List.generate(_pages.length, (i) {
          final showBadge = i == 3 && pendingCount > 0;
          return NavigationDestination(
            icon: Badge(
              isLabelVisible: showBadge,
              label: showBadge ? Text(pendingCount.toString()) : null,
              child: Icon(_icons[i]),
            ),
            selectedIcon: Icon(_activeIcons[i]),
            label: _labels[i],
          );
        }),
      ),
    );
  }
}
