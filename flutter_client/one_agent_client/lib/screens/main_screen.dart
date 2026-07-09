import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../providers/approval_provider.dart';
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
    // 延迟启动审批轮询，等设置加载完成
    WidgetsBinding.instance.addPostFrameCallback((_) {
      ref.read(approvalProvider.notifier).startPolling();
    });
  }

  @override
  void dispose() {
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
