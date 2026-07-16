import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/system_api.dart';
import '../providers/settings_provider.dart';
import '../providers/server_config_provider.dart';
import '../providers/system_provider.dart';
import '../providers/update_provider.dart';

/// 设置页面 — 统一管理所有 One-Agent 设置
///
/// 设计原则：简洁、美观、科技感
/// 所有服务端配置（agent / llm / router / memory / execution / security /
/// monitoring / cache）与客户端配置（连接、更新）统一在此页面管理。
class SettingsScreen extends ConsumerWidget {
  const SettingsScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final settingsState = ref.watch(settingsProvider);
    final isConnected = settingsState.isConnected;

    return Scaffold(
      appBar: AppBar(
        title: const Text('设置'),
        actions: [
          if (isConnected)
            IconButton(
              icon: const Icon(Icons.refresh),
              onPressed: () =>
                  ref.read(serverConfigProvider.notifier).loadConfig(),
            ),
        ],
      ),
      body: isConnected
          ? const _UnifiedSettingsView()
          : const _ConnectionSetupView(),
    );
  }
}

// ════════════════════════════════════════════════════════════════
//  连接设置视图（未连接时显示）
// ════════════════════════════════════════════════════════════════
class _ConnectionSetupView extends ConsumerStatefulWidget {
  const _ConnectionSetupView();

  @override
  ConsumerState<_ConnectionSetupView> createState() =>
      _ConnectionSetupViewState();
}

class _ConnectionSetupViewState extends ConsumerState<_ConnectionSetupView> {
  late final TextEditingController _urlController;
  late final TextEditingController _keyController;
  bool _obscureKey = true;

  @override
  void initState() {
    super.initState();
    _urlController = TextEditingController();
    _keyController = TextEditingController();
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final s = ref.read(settingsProvider);
    if (_urlController.text != s.baseUrl) _urlController.text = s.baseUrl;
    if (_keyController.text != s.apiKey) _keyController.text = s.apiKey;
  }

  @override
  void dispose() {
    _urlController.dispose();
    _keyController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final s = ref.watch(settingsProvider);

    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 420),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              // Logo / 标题
              Container(
                width: 72,
                height: 72,
                decoration: BoxDecoration(
                  gradient: LinearGradient(
                    colors: [
                      theme.colorScheme.primary,
                      theme.colorScheme.tertiary,
                    ],
                  ),
                  borderRadius: BorderRadius.circular(20),
                ),
                child: const Icon(Icons.hub, size: 36, color: Colors.white),
              ),
              const SizedBox(height: 16),
              Text('One-Agent', style: theme.textTheme.headlineSmall),
              const SizedBox(height: 4),
              Text(
                '连接到你的 AI Agent 服务器',
                style: theme.textTheme.bodyMedium?.copyWith(
                  color: theme.colorScheme.onSurfaceVariant,
                ),
              ),
              const SizedBox(height: 32),

              // 服务器地址
              TextField(
                controller: _urlController,
                keyboardType: TextInputType.url,
                decoration: _inputDecoration(
                  '服务器地址',
                  'http://192.168.1.100:18792',
                  Icons.link,
                ),
                onSubmitted: (v) => v.trim().isNotEmpty
                    ? ref.read(settingsProvider.notifier).setBaseUrl(v.trim())
                    : null,
              ),
              const SizedBox(height: 16),

              // API Key
              TextField(
                controller: _keyController,
                obscureText: _obscureKey,
                decoration: _inputDecoration(
                  'API Key',
                  '请输入 API Key',
                  Icons.key,
                ).copyWith(
                  suffixIcon: IconButton(
                    icon: Icon(_obscureKey
                        ? Icons.visibility_off
                        : Icons.visibility),
                    onPressed: () => setState(() => _obscureKey = !_obscureKey),
                  ),
                ),
                onSubmitted: (v) => v.trim().isNotEmpty
                    ? ref.read(settingsProvider.notifier).setApiKey(v.trim())
                    : null,
              ),
              const SizedBox(height: 24),

              // 测试连接按钮
              SizedBox(
                width: double.infinity,
                height: 48,
                child: FilledButton.icon(
                  onPressed: s.isLoading
                      ? null
                      : () async {
                          await ref
                              .read(settingsProvider.notifier)
                              .setBaseUrl(_urlController.text.trim());
                          await ref
                              .read(settingsProvider.notifier)
                              .setApiKey(_keyController.text.trim());
                          final ok = await ref
                              .read(settingsProvider.notifier)
                              .checkConnection();
                          if (context.mounted) {
                            ScaffoldMessenger.of(context).showSnackBar(
                              SnackBar(
                                content: Text(ok ? '连接成功' : '连接失败'),
                                behavior: SnackBarBehavior.floating,
                              ),
                            );
                          }
                        },
                  icon: s.isLoading
                      ? const SizedBox(
                          width: 18,
                          height: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.wifi_tethering),
                  label: const Text('测试连接'),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  InputDecoration _inputDecoration(String label, String hint, IconData icon) {
    return InputDecoration(
      labelText: label,
      hintText: hint,
      prefixIcon: Icon(icon),
      border: OutlineInputBorder(borderRadius: BorderRadius.circular(12)),
    );
  }
}

// ════════════════════════════════════════════════════════════════
//  统一设置视图（已连接时显示）
// ════════════════════════════════════════════════════════════════
class _UnifiedSettingsView extends ConsumerStatefulWidget {
  const _UnifiedSettingsView();

  @override
  ConsumerState<_UnifiedSettingsView> createState() =>
      _UnifiedSettingsViewState();
}

class _UnifiedSettingsViewState extends ConsumerState<_UnifiedSettingsView> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      ref.read(serverConfigProvider.notifier).loadConfig();
    });
  }

  @override
  Widget build(BuildContext context) {
    final cfgState = ref.watch(serverConfigProvider);
    final notifier = ref.read(serverConfigProvider.notifier);

    if (cfgState.isLoading) {
      return const Center(child: CircularProgressIndicator());
    }

    if (cfgState.error != null && cfgState.config == null) {
      return _ErrorView(
        error: cfgState.error!,
        onRetry: () => ref.read(serverConfigProvider.notifier).loadConfig(),
      );
    }

    return Stack(
      children: [
        RefreshIndicator(
          onRefresh: () => notifier.loadConfig(),
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
            children: [
              _ModelRoutingSection(notifier: notifier, state: cfgState),
              const SizedBox(height: 16),
              _AgentSection(notifier: notifier),
              const SizedBox(height: 16),
              _MemorySection(notifier: notifier),
              const SizedBox(height: 16),
              _ExecutionSection(notifier: notifier),
              const SizedBox(height: 16),
              _CostSection(notifier: notifier),
              const SizedBox(height: 16),
              _SecuritySection(notifier: notifier),
              const SizedBox(height: 16),
              _AdvancedSection(notifier: notifier),
              const SizedBox(height: 16),
              _ConnectionInfoSection(ref: ref),
              const SizedBox(height: 16),
              const _AppearanceSection(),
              const SizedBox(height: 16),
              const _AboutSection(),
            ],
          ),
        ),
        if (cfgState.isSaving)
          Positioned(
            top: 0,
            left: 0,
            right: 0,
            child: LinearProgressIndicator(
              backgroundColor: Theme.of(context).colorScheme.surface,
            ),
          ),
      ],
    );
  }
}

// ════════════════════════════════════════════════════════════════
//  可复用设置组件
// ════════════════════════════════════════════════════════════════

/// 设置分区卡片
class _SettingsSection extends StatelessWidget {
  final IconData icon;
  final String title;
  final List<Widget> children;
  final Widget? trailing;

  const _SettingsSection({
    required this.icon,
    required this.title,
    required this.children,
    this.trailing,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Card(
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(16),
        side: BorderSide(
          color: theme.colorScheme.outlineVariant.withOpacity(0.3),
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 16, 16, 8),
            child: Row(
              children: [
                Icon(icon, size: 20, color: theme.colorScheme.primary),
                const SizedBox(width: 10),
                Text(
                  title,
                  style: theme.textTheme.titleSmall?.copyWith(
                    fontWeight: FontWeight.w600,
                    letterSpacing: 0.3,
                  ),
                ),
                const Spacer(),
                if (trailing != null) trailing!,
              ],
            ),
          ),
          const Divider(height: 1, indent: 20, endIndent: 20),
          ...children,
        ],
      ),
    );
  }
}

/// 开关项
class _SwitchTile extends StatelessWidget {
  final String title;
  final String? subtitle;
  final bool value;
  final ValueChanged<bool>? onChanged;

  const _SwitchTile({
    required this.title,
    this.subtitle,
    required this.value,
    this.onChanged,
  });

  @override
  Widget build(BuildContext context) {
    return SwitchListTile(
      title: Text(title),
      subtitle: subtitle != null
          ? Text(subtitle!, style: const TextStyle(fontSize: 12))
          : null,
      value: value,
      onChanged: onChanged,
      contentPadding: const EdgeInsets.symmetric(horizontal: 20),
    );
  }
}

/// 导航/编辑项
class _NavTile extends StatelessWidget {
  final String title;
  final String? value;
  final String? subtitle;
  final IconData? leading;
  final VoidCallback? onTap;

  const _NavTile({
    required this.title,
    this.value,
    this.subtitle,
    this.leading,
    this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return ListTile(
      leading: leading != null ? Icon(leading, size: 20) : null,
      title: Text(title),
      subtitle: subtitle != null
          ? Text(subtitle!, style: const TextStyle(fontSize: 12))
          : null,
      trailing: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (value != null)
            Flexible(
              child: Text(
                value!,
                style: TextStyle(
                  fontSize: 13,
                  fontFamily: 'monospace',
                  color: Theme.of(context).colorScheme.onSurfaceVariant,
                ),
                overflow: TextOverflow.ellipsis,
              ),
            ),
          const SizedBox(width: 4),
          const Icon(Icons.chevron_right, size: 20),
        ],
      ),
      onTap: onTap,
      contentPadding: const EdgeInsets.symmetric(horizontal: 20),
    );
  }
}

/// 信息标签
class _InfoChip extends StatelessWidget {
  final String label;
  final IconData? icon;
  final Color? color;

  const _InfoChip(this.label, {this.icon, this.color});

  @override
  Widget build(BuildContext context) {
    final c = color ?? Theme.of(context).colorScheme.primary;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: c.withOpacity(0.1),
        borderRadius: BorderRadius.circular(6),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (icon != null) ...[
            Icon(icon, size: 12, color: c),
            const SizedBox(width: 4),
          ],
          Text(
            label,
            style: TextStyle(fontSize: 11, color: c, fontWeight: FontWeight.w500),
          ),
        ],
      ),
    );
  }
}

// ════════════════════════════════════════════════════════════════
//  模型与路由分区
// ════════════════════════════════════════════════════════════════
class _ModelRoutingSection extends StatelessWidget {
  final ServerConfigNotifier notifier;
  final ServerConfigState state;

  const _ModelRoutingSection({required this.notifier, required this.state});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    // 问题1+3 修复：使用 routingEnabled（从 state.config 读取）而非
    // catalogRoutingEnabled（从 state.models 读取）。updateConfig 后
    // state.config 立即更新，但 state.models 需要等 loadModels() 完成
    // 才刷新，导致开关状态不同步（按钮一直显示开启）。
    final routingOn = notifier.routingEnabled;
    final defaultModel = notifier.catalogDefaultModel;
    final provider = notifier.catalogPrimaryProvider;
    final tiers = notifier.tierData;
    final modelsByCategory = notifier.modelsByCategory;

    return _SettingsSection(
      icon: Icons.psychology,
      title: '模型与智能路由',
      trailing: _InfoChip(
        routingOn ? '4层路由' : '单模型',
        icon: routingOn ? Icons.auto_awesome : Icons.circle_outlined,
      ),
      children: [
        // ── 服务商管理 ─────────────────────────────
        _ProviderManagementArea(notifier: notifier),
        const Divider(height: 1, indent: 20, endIndent: 20),

        // ── 默认模型 Hero ───────────────────────────
        // 问题2 修复：点击默认模型名称弹出所有模型列表供选择
        // 之前只显示模型名称，无法切换
        Container(
          margin: const EdgeInsets.all(16),
          padding: const EdgeInsets.all(20),
          decoration: BoxDecoration(
            gradient: LinearGradient(
              begin: Alignment.topLeft,
              end: Alignment.bottomRight,
              colors: [
                theme.colorScheme.primary.withOpacity(0.08),
                theme.colorScheme.tertiary.withOpacity(0.05),
              ],
            ),
            borderRadius: BorderRadius.circular(14),
            border: Border.all(
              color: theme.colorScheme.primary.withOpacity(0.15),
            ),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Icon(Icons.memory, size: 18, color: theme.colorScheme.primary),
                  const SizedBox(width: 8),
                  Text(
                    '默认模型',
                    style: theme.textTheme.labelMedium?.copyWith(
                      color: theme.colorScheme.primary,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const Spacer(),
                  _TestModelButton(
                    modelId: defaultModel,
                    enabled: defaultModel.isNotEmpty,
                  ),
                ],
              ),
              const SizedBox(height: 12),
              // 问题2：点击模型名称 → 弹出所有模型列表供选择
              InkWell(
                onTap: () => _showModelSelectionDialog(
                  context,
                  title: '默认模型',
                  current: defaultModel,
                  notifier: notifier,
                  onSubmit: (v) {
                    // 解析 provider 前缀，同时更新 primary_model 和 primary_provider
                    final parts = v.split('/');
                    final provider = parts.length > 1 ? parts.first : '';
                    final updates = <String, dynamic>{
                      'llm': {'primary_model': v}
                    };
                    if (provider.isNotEmpty) {
                      (updates['llm'] as Map<String, dynamic>)['primary_provider'] = provider;
                    }
                    return notifier.updateConfig(updates);
                  },
                ),
                borderRadius: BorderRadius.circular(8),
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 4),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.center,
                    children: [
                      Expanded(
                        child: Text(
                          defaultModel.isEmpty ? '点击选择模型' : defaultModel,
                          style: theme.textTheme.titleMedium?.copyWith(
                            fontFamily: 'monospace',
                            fontWeight: FontWeight.bold,
                            color: defaultModel.isEmpty
                                ? theme.colorScheme.onSurfaceVariant
                                : null,
                          ),
                        ),
                      ),
                      Icon(Icons.unfold_more,
                          size: 18,
                          color: theme.colorScheme.primary.withOpacity(0.7)),
                    ],
                  ),
                ),
              ),
              if (provider.isNotEmpty) ...[
                const SizedBox(height: 4),
                Text(
                  'Provider: $provider',
                  style: theme.textTheme.bodySmall?.copyWith(
                    color: theme.colorScheme.onSurfaceVariant,
                  ),
                ),
              ],
              const SizedBox(height: 16),
              Row(
                children: [
                  _InfoChip('温度 ${notifier.defaultTemperature}'),
                  const SizedBox(width: 8),
                  _InfoChip('MaxTokens ${notifier.defaultMaxTokens}'),
                  const SizedBox(width: 8),
                  _InfoChip('超时 ${notifier.llmTimeout}s'),
                ],
              ),
            ],
          ),
        ),

        // 4 层智能路由开关
        _SwitchTile(
          title: '4 层智能路由',
          subtitle: routingOn
              ? '根据任务复杂度自动选择 trivial → simple → complex → expert'
              : '关闭后所有请求使用默认模型',
          value: routingOn,
          onChanged: (v) async {
            final ok = await notifier.updateConfig({
              'router': {'enabled': v}
            });
            if (context.mounted) {
              ScaffoldMessenger.of(context).showSnackBar(
                SnackBar(
                  content: Text(ok
                      ? '智能路由已${v ? '开启' : '关闭'}'
                      : notifier.state.error ?? '保存失败'),
                  behavior: SnackBarBehavior.floating,
                ),
              );
            }
            if (ok) await notifier.loadModels();
          },
        ),

        // 路由子选项
        // 问题2 修复：这三个开关（自我进化/上下文压缩/技能懒加载）在配置层
        // 面是独立于 router.enabled 的，后端也独立读取各自的 enabled 字段。
        // 之前用 if (routingOn) 包裹导致路由关闭后这三个开关从 UI 消失，
        // 用户无法查看和操作它们。现在始终显示，路由关闭时加提示。
        _SwitchTile(
          title: '自我进化',
          subtitle: routingOn
              ? '根据历史成功率自动调整 tier 阈值'
              : '根据历史成功率自动调整 tier 阈值（需开启 4 层路由生效）',
          value: notifier.selfEvolutionEnabled,
          onChanged: (v) => notifier.updateConfig({
            'router': {'self_evolution': {'enabled': v}}
          }).then((ok) => _showResult(context, ok, notifier, '自我进化')),
        ),
        _SwitchTile(
          title: '上下文压缩',
          subtitle: routingOn
              ? '超长对话自动压缩历史上下文'
              : '超长对话自动压缩历史上下文（需开启 4 层路由生效）',
          value: notifier.contextCompressionEnabled,
          onChanged: (v) => notifier.updateConfig({
            'router': {'context_compression': {'enabled': v}}
          }).then((ok) => _showResult(context, ok, notifier, '上下文压缩')),
        ),
        _SwitchTile(
          title: '技能懒加载',
          subtitle: routingOn
              ? '按需加载技能，降低 token 消耗'
              : '按需加载技能，降低 token 消耗（需开启 4 层路由生效）',
          value: notifier.skillLazyLoadingEnabled,
          onChanged: (v) => notifier.updateConfig({
            'router': {'skill_lazy_loading': {'enabled': v}}
          }).then((ok) => _showResult(context, ok, notifier, '技能懒加载')),
        ),

        // 4 层路由分布
        if (routingOn && tiers != null) ...[
          const Divider(height: 1, indent: 20, endIndent: 20),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 12, 20, 8),
            child: Text(
              'TIER 分布',
              style: theme.textTheme.labelSmall?.copyWith(
                color: theme.colorScheme.onSurfaceVariant,
                letterSpacing: 1,
              ),
            ),
          ),
          ...tiers.entries.map((e) {
            final tier = e.value as Map<String, dynamic>?;
            if (tier == null) return const SizedBox.shrink();
            final models =
                (tier['models'] as List? ?? []).whereType<String>().toList();
            final picked = (tier['stats'] as Map?)?['picked'] ?? 0;
            return _TierRow(
              name: e.key,
              modelCount: models.length,
              threshold: (tier['threshold'] as num?)?.toDouble() ?? 0,
              tokenBudget: tier['token_budget'] ?? 0,
              picked: picked,
              tierModels: models,
              onSelectModels: () => _showTierModelsDialog(
                context,
                tierName: e.key,
                currentModels: models,
                notifier: notifier,
              ),
            );
          }),
        ],

        // 模型分类
        if (modelsByCategory != null && modelsByCategory.isNotEmpty) ...[
          const Divider(height: 1, indent: 20, endIndent: 20),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 12, 20, 8),
            child: Text(
              '模型分类',
              style: theme.textTheme.labelSmall?.copyWith(
                color: theme.colorScheme.onSurfaceVariant,
                letterSpacing: 1,
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 16),
            child: Wrap(
              spacing: 8,
              runSpacing: 8,
              children: modelsByCategory.entries.map((e) {
                final modelIds = (e.value as List)
                    .whereType<String>()
                    .toList();
                return _CategoryChip(
                  category: e.key,
                  count: modelIds.length,
                  modelIds: modelIds,
                );
              }).toList(),
            ),
          ),
        ] else ...[
          const SizedBox(height: 8),
        ],

        // ── 轻量模型选择区 ──────────────────────────
        // 问题5 修复：
        // - 移除多余默认模型栏（Hero 区已有，避免重复）
        // - 修复 Column(crossAxisAlignment: start) 导致的竖列显示问题
        // - 轻量模型用 _showModelSelectionDialog 弹窗选择（与默认模型一致），
        //   而不是让用户手动修改模型名称
        Container(
          margin: const EdgeInsets.fromLTRB(16, 4, 16, 16),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(12),
            border: Border.all(
              color: theme.colorScheme.outlineVariant.withOpacity(0.5),
            ),
            color: theme.colorScheme.surfaceContainerHighest.withOpacity(0.25),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 10, 16, 2),
                child: Row(
                  children: [
                    Icon(Icons.tune, size: 14,
                        color: theme.colorScheme.onSurfaceVariant),
                    const SizedBox(width: 6),
                    Text(
                      '轻量模型（LLM 意图分类用）',
                      style: theme.textTheme.labelSmall?.copyWith(
                        color: theme.colorScheme.onSurfaceVariant,
                        letterSpacing: 0.5,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ],
                ),
              ),
              _NavTile(
                title: '轻量模型',
                subtitle: '路由摘要、意图分类用',
                value: notifier.lightweightModel,
                leading: Icons.flash_on,
                onTap: () => _showModelSelectionDialog(
                  context,
                  title: '轻量模型',
                  current: notifier.lightweightModel,
                  notifier: notifier,
                  onSubmit: (v) => notifier.updateConfig({
                    'llm': {'lightweight_model': v}
                  }),
                ),
              ),
            ],
          ),
        ),

        // 温度
        _NavTile(
          title: 'Temperature',
          value: notifier.defaultTemperature.toString(),
          leading: Icons.thermostat,
          onTap: () => _showSliderDialog(
            context,
            title: 'Temperature',
            initial: notifier.defaultTemperature,
            min: 0,
            max: 2,
            divisions: 20,
            onSubmit: (v) => notifier.updateConfig({
              'llm': {'default_temperature': v}
            }),
          ),
        ),
        // MaxTokens
        _NavTile(
          title: 'Max Tokens',
          value: notifier.defaultMaxTokens.toString(),
          leading: Icons.text_fields,
          onTap: () => _showNumberEditDialog(
            context,
            title: 'Max Tokens',
            initial: notifier.defaultMaxTokens,
            min: 1,
            onSubmit: (v) => notifier.updateConfig({
              'llm': {'default_max_tokens': v}
            }),
          ),
        ),
        // 语义缓存
        _SwitchTile(
          title: '语义缓存',
          subtitle: '相似请求复用结果（阈值 ${notifier.semanticCacheThreshold}）',
          value: notifier.semanticCacheEnabled,
          onChanged: (v) => notifier.updateConfig({
            'llm': {'semantic_cache': {'enabled': v}}
          }).then((ok) => _showResult(context, ok, notifier, '语义缓存')),
        ),
      ],
    );
  }
}

/// 服务商管理区域 — 显示已配置服务商 + 添加入口 + 一键测试
///
/// 问题2a/2b/2c 修复：
/// - 2a: 所有有 key 的服务商都保留显示（从 config.llm.api_keys 读取）
/// - 2b: 添加"测试全部"按钮，绿色=可用，灰色=不可用
/// - 2c: 拉取到的模型数据缓存在 ProviderModelCache 中，详情页可复用
class _ProviderManagementArea extends StatefulWidget {
  final ServerConfigNotifier notifier;

  const _ProviderManagementArea({required this.notifier});

  @override
  State<_ProviderManagementArea> createState() => _ProviderManagementAreaState();
}

class _ProviderManagementAreaState extends State<_ProviderManagementArea> {
  /// 服务商测试状态：null=未测试, true=可用, false=不可用
  final Map<String, bool> _testStatus = {};
  bool _testingAll = false;

  /// 提取当前已配置的服务商列表
  /// 问题2a 修复：优先使用 config.llm.api_keys 中已配置 key 的服务商，
  /// 确保切换主服务商后新增服务商不会消失。
  List<String> _extractConfiguredProviders() {
    final providers = <String>{};
    // 1. 从已配置 API Key 的服务商中提取（最可靠的来源）
    providers.addAll(widget.notifier.configuredProviders);
    // 2. 从 primary_provider 补充
    final primary = widget.notifier.catalogPrimaryProvider;
    if (primary.isNotEmpty) providers.add(primary);
    // 3. 从 available_models 补充（可能有未在 api_keys 中的服务商）
    final models = widget.notifier.availableModels ?? [];
    for (final m in models) {
      if (m is Map<String, dynamic>) {
        final p = m['provider'] as String?;
        if (p != null && p.isNotEmpty) providers.add(p);
      }
    }
    return providers.toList()..sort();
  }

  /// 问题2b：一键测试所有已配置 key 的服务商
  Future<void> _testAllProviders() async {
    final providersToTest = widget.notifier.configuredProviders;
    if (providersToTest.isEmpty) return;
    setState(() {
      _testingAll = true;
      _testStatus.clear();
    });
    for (final p in providersToTest) {
      final result = await SystemApi.testProvider(provider: p);
      if (!mounted) return;
      final ok = result?['ok'] == true;
      setState(() {
        _testStatus[p] = ok;
      });
      // 问题2c：缓存拉取到的模型数据
      if (ok) {
        final freeModels = (result?['free_models'] as List?)
                ?.whereType<Map<String, dynamic>>()
                .toList() ??
            [];
        final paidModels = (result?['paid_models'] as List?)
                ?.whereType<Map<String, dynamic>>()
                .toList() ??
            [];
        ProviderModelCache.set(p, free: freeModels, paid: paidModels);
      }
    }
    if (!mounted) return;
    setState(() => _testingAll = false);
    final okCount = _testStatus.values.where((v) => v).length;
    final failCount = _testStatus.length - okCount;
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text('测试完成：$okCount 个可用'
              '${failCount > 0 ? '，$failCount 个不可用' : ''}'),
          behavior: SnackBarBehavior.floating,
        ),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final notifier = widget.notifier;
    final providers = _extractConfiguredProviders();
    final primary = notifier.catalogPrimaryProvider;
    final configuredKeyProviders = notifier.configuredProviders.toSet();

    return Padding(
      padding: const EdgeInsets.fromLTRB(20, 14, 12, 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.cloud_outlined,
                  size: 16, color: theme.colorScheme.primary),
              const SizedBox(width: 6),
              Text(
                '服务商',
                style: theme.textTheme.labelMedium?.copyWith(
                  color: theme.colorScheme.onSurfaceVariant,
                  letterSpacing: 0.5,
                  fontWeight: FontWeight.w600,
                ),
              ),
              if (providers.isNotEmpty) ...[
                const SizedBox(width: 8),
                Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
                  decoration: BoxDecoration(
                    color: theme.colorScheme.primary.withOpacity(0.1),
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Text(
                    '${providers.length}',
                    style: TextStyle(
                      fontSize: 11,
                      color: theme.colorScheme.primary,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                ),
              ],
              const SizedBox(width: 6),
              if (providers.isNotEmpty)
                Tooltip(
                  message: '长按设为主服务商 · 点击查看详情',
                  child: Icon(Icons.info_outline,
                      size: 12, color: theme.colorScheme.outline),
                ),
              const Spacer(),
              // 问题2b：一键测试所有服务商状态
              if (configuredKeyProviders.isNotEmpty)
                TextButton.icon(
                  icon: _testingAll
                      ? const SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(strokeWidth: 2))
                      : const Icon(Icons.network_check, size: 16),
                  label: Text(
                      _testingAll ? '测试中...' : '测试全部',
                      style: const TextStyle(fontSize: 12)),
                  onPressed: _testingAll ? null : _testAllProviders,
                  style: TextButton.styleFrom(
                    padding:
                        const EdgeInsets.symmetric(horizontal: 8, vertical: 0),
                    minimumSize: const Size(0, 30),
                    tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                  ),
                ),
              TextButton.icon(
                icon: const Icon(Icons.add, size: 16),
                label: const Text('添加服务商',
                    style: TextStyle(fontSize: 12)),
                onPressed: () =>
                    _showProviderManagementDialog(context, notifier),
                style: TextButton.styleFrom(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 8, vertical: 0),
                  minimumSize: const Size(0, 30),
                  tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          if (providers.isEmpty)
            Text(
              '尚未配置服务商 — 点击右上角添加',
              style: theme.textTheme.bodySmall?.copyWith(
                color: theme.colorScheme.onSurfaceVariant,
                fontStyle: FontStyle.italic,
              ),
            )
          else
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: providers
                  .map((p) => _ProviderPill(
                        name: p,
                        isPrimary: p == primary,
                        hasKey: configuredKeyProviders.contains(p),
                        // 问题2b：显示测试状态（绿色=可用，灰色=不可用）
                        isAvailable: _testStatus[p],
                        onTap: () => _showProviderDetailSheet(
                          context: context,
                          name: p,
                          notifier: notifier,
                        ),
                        onLongPress: p == primary
                            ? null
                            : () async {
                                final ok = await notifier.updateConfig({
                                  'llm': {'primary_provider': p}
                                });
                                if (context.mounted) {
                                  ScaffoldMessenger.of(context).showSnackBar(
                                    SnackBar(
                                      content: Text(ok
                                          ? '已设 $p 为主服务商'
                                          : notifier.state.error ?? '设置失败'),
                                      behavior: SnackBarBehavior.floating,
                                    ),
                                  );
                                }
                                // 问题1 修复：切换主服务商后用 loadConfig()
                                // 而非 loadModels()，确保 state.config 保留
                                // 所有 api_keys（包括新增服务商），避免消失。
                                if (ok) await notifier.loadConfig();
                              },
                      ))
                  .toList(),
            ),
        ],
      ),
    );
  }
}

/// 服务商胶囊（小标签，点击查看详情，长按设为主服务商）
class _ProviderPill extends StatelessWidget {
  final String name;
  final bool isPrimary;
  final bool hasKey;
  /// 问题2b：测试状态。null=未测试, true=可用(绿), false=不可用(灰)
  final bool? isAvailable;
  final VoidCallback? onTap;
  final VoidCallback? onLongPress;

  const _ProviderPill({
    required this.name,
    this.isPrimary = false,
    this.hasKey = false,
    this.isAvailable,
    this.onTap,
    this.onLongPress,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final color = isPrimary
        ? theme.colorScheme.primary
        : theme.colorScheme.onSurfaceVariant;
    return InkWell(
      onTap: onTap,
      onLongPress: onLongPress,
      borderRadius: BorderRadius.circular(8),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
        decoration: BoxDecoration(
          color: color.withOpacity(0.08),
          borderRadius: BorderRadius.circular(8),
          border: Border.all(color: color.withOpacity(0.3)),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(isPrimary ? Icons.star : Icons.cloud,
                size: 12, color: color),
            const SizedBox(width: 4),
            Text(
              name,
              style: TextStyle(
                fontSize: 12,
                color: color,
                fontWeight: FontWeight.w500,
                fontFamily: 'monospace',
              ),
            ),
            // 问题2b：测试状态指示灯（绿色=可用，灰色=不可用，无色=未测试）
            if (isAvailable != null) ...[
              const SizedBox(width: 4),
              Tooltip(
                message: isAvailable! ? '可用' : '不可用',
                child: Container(
                  width: 6,
                  height: 6,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    color: isAvailable! ? Colors.green : Colors.grey,
                  ),
                ),
              ),
            ] else ...[
              // key 状态点（未测试时显示 key 配置状态）
              const SizedBox(width: 4),
              Container(
                width: 5,
                height: 5,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: hasKey ? Colors.green : theme.colorScheme.outline,
                ),
              ),
            ],
            if (isPrimary) ...[
              const SizedBox(width: 4),
              Container(
                padding:
                    const EdgeInsets.symmetric(horizontal: 4, vertical: 1),
                decoration: BoxDecoration(
                  color: color,
                  borderRadius: BorderRadius.circular(3),
                ),
                child: Text(
                  '主',
                  style: TextStyle(
                    fontSize: 9,
                    color: theme.colorScheme.onPrimary,
                    fontWeight: FontWeight.bold,
                  ),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

/// 测试模型按钮 — 在 Hero 卡片右上角
class _TestModelButton extends StatefulWidget {
  final String modelId;
  final bool enabled;

  const _TestModelButton({required this.modelId, this.enabled = true});

  @override
  State<_TestModelButton> createState() => _TestModelButtonState();
}

class _TestModelButtonState extends State<_TestModelButton> {
  bool _testing = false;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 32,
      child: FilledButton.tonal(
        onPressed: widget.enabled && !_testing ? _test : null,
        style: FilledButton.styleFrom(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 0),
          minimumSize: const Size(0, 30),
          tapTargetSize: MaterialTapTargetSize.shrinkWrap,
          textStyle: const TextStyle(
              fontSize: 12, fontWeight: FontWeight.w600),
        ),
        child: _testing
            ? const SizedBox(
                width: 14,
                height: 14,
                child: CircularProgressIndicator(strokeWidth: 2),
              )
            : const Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(Icons.network_check, size: 14),
                  SizedBox(width: 4),
                  Text('测试'),
                ],
              ),
      ),
    );
  }

  Future<void> _test() async {
    setState(() => _testing = true);
    final result = await SystemApi.testModel(widget.modelId);
    if (!mounted) return;
    setState(() => _testing = false);
    _showModelTestResult(context, result);
  }
}

/// Tier 行 — 4 层路由每层一行，横向展示模型列表，右侧"选择模型"按钮
class _TierRow extends StatelessWidget {
  final String name;
  final int modelCount;
  final double threshold;
  final int tokenBudget;
  final int picked;
  final List<String> tierModels;
  final VoidCallback? onSelectModels;

  const _TierRow({
    required this.name,
    required this.modelCount,
    required this.threshold,
    required this.tokenBudget,
    required this.picked,
    this.tierModels = const [],
    this.onSelectModels,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colors = {
      'trivial': Colors.green,
      'simple': Colors.blue,
      'complex': Colors.orange,
      'expert': Colors.red,
    };
    final color = colors[name] ?? theme.colorScheme.primary;
    final label = {
      'trivial': '极简',
      'simple': '简单',
      'complex': '复杂',
      'expert': '专家',
    }[name] ?? name;

    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // 第一行：tier 标签 + 元信息 + 选择按钮
          Row(
            children: [
              Container(
                width: 8,
                height: 8,
                decoration: BoxDecoration(color: color, shape: BoxShape.circle),
              ),
              const SizedBox(width: 10),
              SizedBox(
                width: 56,
                child: Text(label, style: theme.textTheme.bodyMedium),
              ),
              Expanded(
                child: Text(
                  '阈值≥$threshold · ${tokenBudget}T · $picked 次',
                  style: theme.textTheme.bodySmall?.copyWith(
                    fontFamily: 'monospace',
                    color: theme.colorScheme.onSurfaceVariant,
                  ),
                ),
              ),
              if (onSelectModels != null)
                _SelectModelsButton(
                  count: modelCount,
                  onPressed: onSelectModels!,
                ),
            ],
          ),
          // 第二行：横向滚动展示该层已有模型列表
          if (tierModels.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(left: 18, top: 6),
              child: SizedBox(
                height: 28,
                child: ListView.separated(
                  scrollDirection: Axis.horizontal,
                  itemCount: tierModels.length,
                  separatorBuilder: (_, __) => const SizedBox(width: 6),
                  itemBuilder: (context, i) {
                    final m = tierModels[i];
                    // 简化显示：只取 model 名（去掉 provider 前缀）
                    final short = m.contains('/') ? m.split('/').last : m;
                    return Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 8, vertical: 4),
                      decoration: BoxDecoration(
                        color: color.withOpacity(0.08),
                        borderRadius: BorderRadius.circular(6),
                        border: Border.all(color: color.withOpacity(0.25)),
                      ),
                      child: Text(
                        short,
                        style: theme.textTheme.labelSmall?.copyWith(
                          fontFamily: 'monospace',
                          fontSize: 11,
                          color: color,
                        ),
                      ),
                    );
                  },
                ),
              ),
            ),
        ],
      ),
    );
  }
}

/// "选择模型"小按钮
class _SelectModelsButton extends StatelessWidget {
  final int count;
  final VoidCallback onPressed;

  const _SelectModelsButton({required this.count, required this.onPressed});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return InkWell(
      onTap: onPressed,
      borderRadius: BorderRadius.circular(6),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        decoration: BoxDecoration(
          color: theme.colorScheme.primary.withOpacity(0.08),
          borderRadius: BorderRadius.circular(6),
          border: Border.all(
            color: theme.colorScheme.primary.withOpacity(0.3),
          ),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.list_alt,
                size: 12, color: theme.colorScheme.primary),
            const SizedBox(width: 4),
            Text(
              '选择($count)',
              style: TextStyle(
                fontSize: 11,
                color: theme.colorScheme.primary,
                fontWeight: FontWeight.w500,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

/// Tier 颜色徽标（用于模型列表）
class _TierBadge extends StatelessWidget {
  final String tier;
  const _TierBadge({required this.tier});

  static const _colors = {
    'trivial': Colors.green,
    'simple': Colors.blue,
    'complex': Colors.orange,
    'expert': Colors.red,
    'free': Colors.teal,
    'trial': Colors.cyan,
    'standard': Colors.indigo,
    'premium': Colors.purple,
  };

  static const _labels = {
    'trivial': '极简',
    'simple': '简单',
    'complex': '复杂',
    'expert': '专家',
    'free': '免费',
    'trial': '试用',
    'standard': '标准',
    'premium': '高级',
  };

  @override
  Widget build(BuildContext context) {
    final color = _colors[tier] ?? Colors.grey;
    final label = _labels[tier] ?? tier;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: color.withOpacity(0.15),
        borderRadius: BorderRadius.circular(4),
        border: Border.all(color: color.withOpacity(0.4), width: 0.5),
      ),
      child: Text(
        label,
        style: TextStyle(
          fontSize: 10,
          color: color,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}

/// 能力图标标签（用于模型列表）
class _CapabilityChip extends StatelessWidget {
  final String cap;
  const _CapabilityChip({required this.cap});

  static const _icons = {
    'text': Icons.text_snippet,
    'vision': Icons.visibility,
    'image_generation': Icons.image,
    'video': Icons.videocam,
    'audio_in': Icons.mic,
    'audio_out': Icons.speaker,
    'embeddings': Icons.data_object,
    'code': Icons.code,
    'tools': Icons.build,
    'reasoning': Icons.lightbulb,
    'long_context': Icons.expand,
    'json_mode': Icons.data_array,
  };

  static const _labels = {
    'text': '文本',
    'vision': '视觉',
    'image_generation': '绘图',
    'video': '视频',
    'audio_in': '语音入',
    'audio_out': '语音出',
    'embeddings': '嵌入',
    'code': '代码',
    'tools': '工具',
    'reasoning': '推理',
    'long_context': '长上下文',
    'json_mode': 'JSON',
  };

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final icon = _icons[cap] ?? Icons.category;
    final label = _labels[cap] ?? cap;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 2),
      decoration: BoxDecoration(
        color: theme.colorScheme.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(4),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 10, color: theme.colorScheme.primary),
          const SizedBox(width: 2),
          Text(
            label,
            style: TextStyle(
              fontSize: 10,
              color: theme.colorScheme.onSurfaceVariant,
            ),
          ),
        ],
      ),
    );
  }
}

/// 分类标签
class _CategoryChip extends StatelessWidget {
  final String category;
  final int count;
  /// 问题4 修复：传入该分类下的模型 id 列表，点击标签时展示
  final List<String> modelIds;

  const _CategoryChip({
    required this.category,
    required this.count,
    required this.modelIds,
  });

  static const _icons = {
    'text': Icons.text_snippet,
    'vision': Icons.visibility,
    'image_generation': Icons.image,
    'video': Icons.videocam,
    'audio_in': Icons.mic,
    'audio_out': Icons.speaker,
    'embeddings': Icons.data_object,
    'code': Icons.code,
    'tools': Icons.build,
    'reasoning': Icons.lightbulb,
  };

  static const _labels = {
    'text': '文本',
    'vision': '视觉理解',
    'image_generation': '图像生成',
    'video': '视频',
    'audio_in': '语音识别',
    'audio_out': '语音合成',
    'embeddings': '嵌入',
    'code': '代码',
    'tools': '工具调用',
    'reasoning': '推理',
  };

  void _showModels(BuildContext context) {
    final theme = Theme.of(context);
    final label = _labels[category] ?? category;
    showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Row(
          children: [
            Icon(_icons[category] ?? Icons.category,
                size: 20, color: theme.colorScheme.primary),
            const SizedBox(width: 8),
            Text('$label ($count)'),
          ],
        ),
        content: SizedBox(
          width: double.maxFinite,
          child: modelIds.isEmpty
              ? const Text('暂无模型')
              : ListView.builder(
                  shrinkWrap: true,
                  itemCount: modelIds.length,
                  itemBuilder: (ctx, i) => ListTile(
                    dense: true,
                    leading: Icon(Icons.memory,
                        size: 16, color: theme.colorScheme.outline),
                    title: Text(
                      modelIds[i],
                      style: const TextStyle(
                          fontFamily: 'monospace', fontSize: 13),
                    ),
                  ),
                ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('关闭'),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final icon = _icons[category] ?? Icons.category;
    final label = _labels[category] ?? category;

    return InkWell(
      onTap: () => _showModels(context),
      borderRadius: BorderRadius.circular(10),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        decoration: BoxDecoration(
          color: theme.colorScheme.surfaceContainerHighest,
          borderRadius: BorderRadius.circular(10),
          border: Border.all(
            color: theme.colorScheme.primary.withOpacity(0.2),
          ),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(icon, size: 16, color: theme.colorScheme.primary),
            const SizedBox(width: 6),
            Text(label, style: theme.textTheme.labelMedium),
            const SizedBox(width: 4),
            Text(
              '$count',
              style: theme.textTheme.labelSmall?.copyWith(
                color: theme.colorScheme.onSurfaceVariant,
                fontWeight: FontWeight.bold,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ════════════════════════════════════════════════════════════════
//  Agent 分区
// ════════════════════════════════════════════════════════════════
class _AgentSection extends StatelessWidget {
  final ServerConfigNotifier notifier;
  const _AgentSection({required this.notifier});

  @override
  Widget build(BuildContext context) {
    return _SettingsSection(
      icon: Icons.smart_toy,
      title: 'Agent',
      children: [
        _NavTile(
          title: '名称',
          value: notifier.agentName,
          leading: Icons.badge,
          onTap: () => _showTextEditDialog(
            context,
            title: 'Agent 名称',
            label: '名称',
            initial: notifier.agentName,
            onSubmit: (v) => notifier.updateConfig({
              'agent': {'name': v}
            }),
          ),
        ),
        _NavTile(
          title: '语言',
          value: notifier.language == 'zh-CN' || notifier.language == 'zh'
              ? '简体中文'
              : 'English',
          leading: Icons.language,
          onTap: () => _showChoiceDialog(
            context,
            title: '语言',
            options: {'zh-CN': '简体中文', 'en': 'English'},
            current: notifier.language,
            onSubmit: (v) => notifier.updateConfig({
              'agent': {'language': v}
            }),
          ),
        ),
        _NavTile(
          title: '日志级别',
          value: notifier.logLevel,
          leading: Icons.list_alt,
          onTap: () => _showChoiceDialog(
            context,
            title: '日志级别',
            options: {
              'DEBUG': 'DEBUG',
              'INFO': 'INFO',
              'WARNING': 'WARNING',
              'ERROR': 'ERROR',
            },
            current: notifier.logLevel,
            onSubmit: (v) => notifier.updateConfig({
              'agent': {'log_level': v.toLowerCase()}
            }),
          ),
        ),
        _NavTile(
          title: '时区',
          value: notifier.timezone,
          leading: Icons.schedule,
          onTap: () => _showTextEditDialog(
            context,
            title: '时区',
            label: '如 Asia/Shanghai',
            initial: notifier.timezone,
            onSubmit: (v) => notifier.updateConfig({
              'agent': {'timezone': v}
            }),
          ),
        ),
      ],
    );
  }
}

// ════════════════════════════════════════════════════════════════
//  记忆分区
// ════════════════════════════════════════════════════════════════
class _MemorySection extends StatelessWidget {
  final ServerConfigNotifier notifier;
  const _MemorySection({required this.notifier});

  @override
  Widget build(BuildContext context) {
    return _SettingsSection(
      icon: Icons.psychology_alt,
      title: '记忆系统',
      children: [
        // 短期记忆
        _NavTile(
          title: '最大对话轮数',
          value: '${notifier.memoryMaxTurns}',
          leading: Icons.history,
          onTap: () => _showNumberEditDialog(
            context,
            title: '最大对话轮数',
            initial: notifier.memoryMaxTurns,
            min: 1,
            onSubmit: (v) => notifier.updateConfig({
              'memory': {
                'short_term': {'max_turns': v}
              }
            }),
          ),
        ),
        _NavTile(
          title: '最大 Token 数',
          value: '${notifier.memoryMaxTokens}',
          leading: Icons.memory,
          onTap: () => _showNumberEditDialog(
            context,
            title: '最大 Token 数',
            initial: notifier.memoryMaxTokens,
            min: 100,
            onSubmit: (v) => notifier.updateConfig({
              'memory': {
                'short_term': {'max_tokens': v}
              }
            }),
          ),
        ),
        const Divider(height: 1, indent: 20, endIndent: 20),
        // 长期记忆
        _SwitchTile(
          title: '长期记忆',
          subtitle: '持久化存储重要事实',
          value: notifier.longTermMemoryEnabled,
          onChanged: (v) => notifier.updateConfig({
            'memory': {
              'long_term': {'enabled': v}
            }
          }).then((ok) => _showResult(context, ok, notifier, '长期记忆')),
        ),
        _NavTile(
          title: '检索结果数',
          value: '${notifier.longTermMaxResults}',
          leading: Icons.search,
          onTap: () => _showNumberEditDialog(
            context,
            title: '检索结果数',
            initial: notifier.longTermMaxResults,
            min: 1,
            onSubmit: (v) => notifier.updateConfig({
              'memory': {
                'long_term': {'max_results': v}
              }
            }),
          ),
        ),
        _SwitchTile(
          title: '记忆衰减',
          subtitle: '按时间降低旧记忆权重',
          value: notifier.memoryDecayEnabled,
          onChanged: (v) => notifier.updateConfig({
            'memory': {
              'long_term': {'decay_enabled': v}
            }
          }).then((ok) => _showResult(context, ok, notifier, '记忆衰减')),
        ),
        const Divider(height: 1, indent: 20, endIndent: 20),
        // 程序性记忆
        _SwitchTile(
          title: '程序性记忆',
          subtitle: '自动从交互中创建技能',
          value: notifier.proceduralMemoryEnabled,
          onChanged: (v) => notifier.updateConfig({
            'memory': {
              'procedural': {'enabled': v}
            }
          }).then((ok) => _showResult(context, ok, notifier, '程序性记忆')),
        ),
        _SwitchTile(
          title: '自动创建技能',
          subtitle: '重复操作自动提取为可复用技能',
          value: notifier.autoCreateSkills,
          onChanged: (v) => notifier.updateConfig({
            'memory': {
              'procedural': {'auto_create_skills': v}
            }
          }).then((ok) => _showResult(context, ok, notifier, '自动创建技能')),
        ),
      ],
    );
  }
}

// ════════════════════════════════════════════════════════════════
//  执行环境分区
// ════════════════════════════════════════════════════════════════
class _ExecutionSection extends StatelessWidget {
  final ServerConfigNotifier notifier;
  const _ExecutionSection({required this.notifier});

  @override
  Widget build(BuildContext context) {
    return _SettingsSection(
      icon: Icons.terminal,
      title: '执行环境',
      children: [
        _SwitchTile(
          title: '本地 Shell',
          subtitle: '允许 Agent 执行系统命令',
          value: notifier.localShellEnabled,
          onChanged: (v) => notifier.updateConfig({
            'execution': {
              'local_shell': {'enabled': v}
            }
          }).then((ok) => _showResult(context, ok, notifier, '本地 Shell')),
        ),
        _SwitchTile(
          title: 'Docker 沙箱',
          subtitle: '在容器中执行代码（${notifier.dockerImage}）',
          value: notifier.dockerEnabled,
          onChanged: (v) => notifier.updateConfig({
            'execution': {
              'docker': {'enabled': v}
            }
          }).then((ok) => _showResult(context, ok, notifier, 'Docker')),
        ),
        _SwitchTile(
          title: '浏览器',
          subtitle: '允许 Agent 操作浏览器',
          value: notifier.browserEnabled,
          onChanged: (v) => notifier.updateConfig({
            'execution': {
              'browser': {'enabled': v}
            }
          }).then((ok) => _showResult(context, ok, notifier, '浏览器')),
        ),
      ],
    );
  }
}

// ════════════════════════════════════════════════════════════════
//  成本追踪分区
// ════════════════════════════════════════════════════════════════
class _CostSection extends StatelessWidget {
  final ServerConfigNotifier notifier;
  const _CostSection({required this.notifier});

  @override
  Widget build(BuildContext context) {
    return _SettingsSection(
      icon: Icons.payments,
      title: '成本与预算',
      children: [
        _SwitchTile(
          title: '成本追踪',
          subtitle: '记录每次 LLM 调用的 token 和费用',
          value: notifier.costTrackingEnabled,
          onChanged: (v) => notifier.updateConfig({
            'llm': {
              'cost_tracking': {'enabled': v}
            }
          }).then((ok) => _showResult(context, ok, notifier, '成本追踪')),
        ),
        _NavTile(
          title: '每日预算 (USD)',
          value: '\$${notifier.dailyBudget.toStringAsFixed(2)}',
          leading: Icons.today,
          onTap: notifier.costTrackingEnabled
              ? () => _showNumberEditDialog(
                    context,
                    title: '每日预算 (USD)',
                    initial: notifier.dailyBudget,
                    min: 0.01,
                    isDouble: true,
                    onSubmit: (v) => notifier.updateConfig({
                      'llm': {
                        'cost_tracking': {'daily_budget': v.toDouble()}
                      }
                    }),
                  )
              : null,
        ),
        _NavTile(
          title: '每月预算 (USD)',
          value: '\$${notifier.monthlyBudget.toStringAsFixed(2)}',
          leading: Icons.calendar_month,
          onTap: notifier.costTrackingEnabled
              ? () => _showNumberEditDialog(
                    context,
                    title: '每月预算 (USD)',
                    initial: notifier.monthlyBudget,
                    min: 0.01,
                    isDouble: true,
                    onSubmit: (v) => notifier.updateConfig({
                      'llm': {
                        'cost_tracking': {'monthly_budget': v.toDouble()}
                      }
                    }),
                  )
              : null,
        ),
      ],
    );
  }
}

// ════════════════════════════════════════════════════════════════
//  安全分区
// ════════════════════════════════════════════════════════════════
class _SecuritySection extends StatelessWidget {
  final ServerConfigNotifier notifier;
  const _SecuritySection({required this.notifier});

  @override
  Widget build(BuildContext context) {
    // 问题5 修复：密码未配置时（system_executor_password 为空），
    // 隐藏"系统执行器"和"危险命令需密码"开关 — 因为没有密码，
    // 所有命令都无密码执行，这两个开关无意义。
    final pwdConfigured = notifier.isPasswordConfigured;
    return _SettingsSection(
      icon: Icons.shield,
      title: '安全',
      children: [
        if (!pwdConfigured)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
            child: Row(
              children: [
                Icon(Icons.info_outline,
                    size: 14, color: Theme.of(context).colorScheme.outline),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    '系统执行器密码未配置，所有命令直接执行（无需密码）',
                    style: Theme.of(context).textTheme.bodySmall?.copyWith(
                          color: Theme.of(context).colorScheme.outline,
                        ),
                  ),
                ),
              ],
            ),
          ),
        if (pwdConfigured) ...[
          _SwitchTile(
            title: '系统执行器',
            subtitle: '允许通过密码执行系统级命令',
            value: notifier.systemExecutorEnabled,
            onChanged: (v) => notifier.updateConfig({
              'security': {'system_executor_enabled': v}
            }).then((ok) => _showResult(context, ok, notifier, '系统执行器')),
          ),
          _SwitchTile(
            title: '危险命令需密码',
            subtitle: '执行高危命令前要求密码验证',
            value: notifier.requirePasswordForDangerous,
            onChanged: (v) => notifier.updateConfig({
              'security': {'require_password_for_dangerous': v}
            }).then((ok) => _showResult(context, ok, notifier, '危险命令保护')),
          ),
        ],
        _NavTile(
          title: '命令超时',
          value: '${notifier.commandTimeoutSeconds}s',
          leading: Icons.timer,
          onTap: () => _showNumberEditDialog(
            context,
            title: '命令超时 (秒)',
            initial: notifier.commandTimeoutSeconds,
            min: 1,
            onSubmit: (v) => notifier.updateConfig({
              'security': {'command_timeout_seconds': v}
            }),
          ),
        ),
      ],
    );
  }
}

// ════════════════════════════════════════════════════════════════
//  高级设置分区（REST / 监控 / 缓存）
// ════════════════════════════════════════════════════════════════
class _AdvancedSection extends StatelessWidget {
  final ServerConfigNotifier notifier;
  const _AdvancedSection({required this.notifier});

  @override
  Widget build(BuildContext context) {
    return _SettingsSection(
      icon: Icons.tune,
      title: '高级',
      children: [
        // LLM 缓存
        _SwitchTile(
          title: 'LLM 响应缓存',
          subtitle: 'TTL ${notifier.llmCacheTtl}s · 最大 ${notifier.llmCacheMaxSize} 条',
          value: notifier.llmCacheEnabled,
          onChanged: (v) => notifier.updateConfig({
            'llm_cache': {'enabled': v}
          }).then((ok) => _showResult(context, ok, notifier, 'LLM 缓存')),
        ),
        // REST API
        _NavTile(
          title: 'REST API',
          value: '${notifier.restHost}:${notifier.restPort}',
          leading: Icons.api,
          onTap: () => _showInfoDialog(
            context,
            title: 'REST API',
            lines: [
              'Host: ${notifier.restHost}',
              'Port: ${notifier.restPort}',
              'Rate Limit: ${notifier.rateLimitPerMinute}/min',
              '通过配置文件修改 host/port 后重启生效',
            ],
          ),
        ),
        // 监控
        _SwitchTile(
          title: 'Prometheus 监控',
          subtitle: '端口 ${notifier.monitoringPort}',
          value: notifier.monitoringEnabled,
          onChanged: (v) => notifier.updateConfig({
            'monitoring': {'enabled': v}
          }).then((ok) => _showResult(context, ok, notifier, '监控')),
        ),
        // LLM 超时与重试
        _NavTile(
          title: 'LLM 超时',
          value: '${notifier.llmTimeout}s',
          leading: Icons.hourglass_empty,
          onTap: () => _showNumberEditDialog(
            context,
            title: 'LLM 超时 (秒)',
            initial: notifier.llmTimeout,
            min: 5,
            onSubmit: (v) => notifier.updateConfig({
              'llm': {'timeout': v}
            }),
          ),
        ),
        _NavTile(
          title: 'LLM 重试次数',
          value: '${notifier.llmRetries}',
          leading: Icons.replay,
          onTap: () => _showNumberEditDialog(
            context,
            title: '重试次数',
            initial: notifier.llmRetries,
            min: 1,
            max: 10,
            onSubmit: (v) => notifier.updateConfig({
              'llm': {'retries': v}
            }),
          ),
        ),
      ],
    );
  }
}

// ════════════════════════════════════════════════════════════════
//  连接信息分区
// ════════════════════════════════════════════════════════════════
class _ConnectionInfoSection extends ConsumerWidget {
  final WidgetRef ref;
  const _ConnectionInfoSection({required this.ref});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final s = ref.watch(settingsProvider);
    final theme = Theme.of(context);
    final connected = s.isConnected;

    return _SettingsSection(
      icon: Icons.wifi,
      title: '连接',
      children: [
        ListTile(
          leading: Icon(
            connected ? Icons.check_circle : Icons.cancel,
            color: connected ? Colors.green : Colors.red,
            size: 20,
          ),
          title: Text(connected ? '已连接' : '未连接'),
          subtitle: Text(
            s.baseUrl,
            style: TextStyle(
              fontSize: 12,
              fontFamily: 'monospace',
              color: theme.colorScheme.onSurfaceVariant,
            ),
          ),
          contentPadding: const EdgeInsets.symmetric(horizontal: 20),
        ),
        _NavTile(
          title: '修改服务器地址',
          leading: Icons.edit,
          onTap: () => _showTextEditDialog(
            context,
            title: '服务器地址',
            label: 'http://host:port',
            initial: s.baseUrl,
            onSubmit: (v) async {
              await ref.read(settingsProvider.notifier).setBaseUrl(v);
              return true;
            },
          ),
        ),
        _NavTile(
          title: '修改 API Key',
          leading: Icons.key,
          onTap: () => _showTextEditDialog(
            context,
            title: 'API Key',
            label: 'API Key',
            initial: s.apiKey,
            obscure: true,
            onSubmit: (v) async {
              await ref.read(settingsProvider.notifier).setApiKey(v);
              return true;
            },
          ),
        ),
        ListTile(
          leading: const Icon(Icons.cleaning_services, size: 20),
          title: const Text('清除缓存'),
          subtitle: const Text('清除客户端缓存', style: TextStyle(fontSize: 12)),
          trailing: const Icon(Icons.chevron_right, size: 20),
          onTap: () async {
            final ok = await ref.read(systemProvider.notifier).clearCache();
            if (context.mounted) {
              ScaffoldMessenger.of(context).showSnackBar(
                SnackBar(
                  content: Text(ok ? '缓存已清除' : '清除失败'),
                  behavior: SnackBarBehavior.floating,
                ),
              );
            }
          },
          contentPadding: const EdgeInsets.symmetric(horizontal: 20),
        ),
      ],
    );
  }
}

// ════════════════════════════════════════════════════════════════
//  外观分区（问题10：客户端文字设置功能）
// ════════════════════════════════════════════════════════════════
class _AppearanceSection extends ConsumerWidget {
  const _AppearanceSection();

  static const _presets = <double>[0.85, 1.0, 1.15, 1.3, 1.5];
  static const _presetLabels = <String>['小', '标准', '中', '大', '特大'];

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final s = ref.watch(settingsProvider);
    final scale = s.fontScale;
    final idx = _presets.indexWhere((p) => (p - scale).abs() < 0.02);
    final label = idx >= 0 ? _presetLabels[idx] : '${(scale * 100).round()}%';

    return _SettingsSection(
      icon: Icons.text_fields,
      title: '外观',
      children: [
        ListTile(
          leading: const Icon(Icons.format_size, size: 20),
          title: const Text('字体大小'),
          subtitle: Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('当前: $label', style: const TextStyle(fontSize: 12)),
                const SizedBox(height: 8),
                Row(
                  children: [
                    const Text('A', style: TextStyle(fontSize: 12)),
                    Expanded(
                      child: Slider(
                        value: scale,
                        min: 0.8,
                        max: 1.6,
                        divisions: 16,
                        label: '${(scale * 100).round()}%',
                        onChanged: (v) {
                          ref.read(settingsProvider.notifier).setFontScale(v);
                        },
                      ),
                    ),
                    const Text('A', style: TextStyle(fontSize: 22)),
                  ],
                ),
                Wrap(
                  spacing: 8,
                  children: [
                    for (var i = 0; i < _presets.length; i++)
                      ChoiceChip(
                        label: Text(_presetLabels[i]),
                        selected: idx == i,
                        onSelected: (_) {
                          ref
                              .read(settingsProvider.notifier)
                              .setFontScale(_presets[i]);
                        },
                      ),
                  ],
                ),
              ],
            ),
          ),
          contentPadding: const EdgeInsets.symmetric(horizontal: 20),
        ),
        ListTile(
          leading: const Icon(Icons.preview, size: 20),
          title: const Text('预览'),
          subtitle: Text(
            '这是一段预览文字，用于查看字体大小效果。The quick brown fox.',
            style: TextStyle(
              fontSize: 14 * scale,
              height: 1.4,
            ),
          ),
          contentPadding: const EdgeInsets.symmetric(horizontal: 20),
        ),
      ],
    );
  }
}

// ════════════════════════════════════════════════════════════════
//  关于分区
// ════════════════════════════════════════════════════════════════
class _AboutSection extends ConsumerWidget {
  const _AboutSection();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final updateState = ref.watch(updateProvider);

    // 问题4 修复：使用 hasUpdate（基于版本号比较）而非 latestRelease != null。
    // 之前 latestRelease 在"当前版本 >= 服务端版本"时为 null，导致 UI 无法
    // 显示服务端最新版本信息。现在始终存储 latestRelease，subtitle 可以同时
    // 展示当前版本和服务端最新版本，让用户清楚知道是否有更新。
    String? subtitle;
    if (updateState.error != null) {
      subtitle = updateState.error;
    } else if (updateState.latestRelease != null) {
      final latest = updateState.latestRelease!;
      if (updateState.hasUpdate) {
        subtitle = '新版本可用: ${latest.tagName} (v${latest.versionNumber})';
      } else {
        // 问题4：当前版本 >= 服务端版本时，仍展示服务端最新版本信息，
        // 让用户知道服务端有什么版本，可手动强制下载。
        subtitle = '服务端最新: ${latest.tagName}（当前已是最新或更新）';
      }
    } else if (updateState.lastCheckedAt != null) {
      final t = updateState.lastCheckedAt!;
      final timeStr =
          '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}';
      subtitle = '已是最新版本（$timeStr 检查）';
    }

    return _SettingsSection(
      icon: Icons.info,
      title: '关于',
      children: [
        ListTile(
          leading: const Icon(Icons.apps, size: 20),
          title: const Text('版本'),
          trailing: Text(
            'v${updateState.currentVersionName} (${updateState.currentVersion})',
            style: const TextStyle(fontFamily: 'monospace', fontSize: 13),
          ),
          contentPadding: const EdgeInsets.symmetric(horizontal: 20),
        ),
        ListTile(
          leading: updateState.isChecking
              ? const SizedBox(
                  width: 18,
                  height: 18,
                  child: CircularProgressIndicator(strokeWidth: 2))
              : const Icon(Icons.system_update, size: 20),
          title: const Text('检查更新'),
          trailing: updateState.isDownloading
              ? SizedBox(
                  width: 80,
                  child: LinearProgressIndicator(
                    value: updateState.downloadProgress,
                  ),
                )
              : const Icon(Icons.chevron_right, size: 20),
          subtitle: subtitle != null
              ? Text(subtitle,
                  style: TextStyle(
                    fontSize: 12,
                    color: updateState.error != null
                        ? Colors.red
                        : updateState.hasUpdate
                            ? Colors.orange
                            : Colors.green,
                  ))
              : null,
          onTap: updateState.isChecking || updateState.isDownloading
              ? null
              : () async {
                  await ref.read(updateProvider.notifier).checkForUpdate();
                  // 检查完成后给出 SnackBar 反馈
                  if (!context.mounted) return;
                  final s = ref.read(updateProvider);
                  final msg = s.error != null
                      ? s.error!
                      : s.hasUpdate
                          ? '发现新版本: ${s.latestRelease!.tagName}'
                          : '已是最新版本';
                  ScaffoldMessenger.of(context).showSnackBar(
                    SnackBar(
                      content: Text(msg),
                      behavior: SnackBarBehavior.floating,
                    ),
                  );
                },
          contentPadding: const EdgeInsets.symmetric(horizontal: 20),
        ),
        // 问题4：发现新版本时显示下载按钮（直接下载，无需进入二级页面）
        if (updateState.hasUpdate && !updateState.isDownloading) ...[
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 20),
            child: FilledButton.icon(
              onPressed: updateState.isChecking
                  ? null
                  : () => ref.read(updateProvider.notifier).downloadAndInstall(),
              icon: const Icon(Icons.download, size: 18),
              label: Text(updateState.latestRelease != null
                  ? '下载 ${updateState.latestRelease!.tagName}'
                  : '下载更新'),
            ),
          ),
          const SizedBox(height: 4),
        ],
        // 问题4：下载中显示进度
        if (updateState.isDownloading) ...[
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 20),
            child: Row(
              children: [
                Expanded(
                  child: LinearProgressIndicator(
                    value: updateState.downloadProgress,
                  ),
                ),
                const SizedBox(width: 8),
                Text(
                  '${(updateState.downloadProgress * 100).toInt()}%',
                  style: const TextStyle(fontSize: 12),
                ),
              ],
            ),
          ),
          const SizedBox(height: 4),
        ],
        // 问题4：即使版本号比较认为"无需更新"，也允许用户强制下载服务端最新版
        // （用于调试或绕过版本比较失误）
        if (updateState.latestRelease != null &&
            !updateState.hasUpdate &&
            !updateState.isDownloading &&
            !updateState.isChecking) ...[
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 20),
            child: OutlinedButton.icon(
              onPressed: () => ref.read(updateProvider.notifier).downloadAndInstall(),
              icon: const Icon(Icons.download_for_offline, size: 18),
              label: const Text('强制下载服务端最新版'),
            ),
          ),
          const SizedBox(height: 4),
        ],
      ],
    );
  }
}

// ════════════════════════════════════════════════════════════════
//  错误视图
// ════════════════════════════════════════════════════════════════
class _ErrorView extends StatelessWidget {
  final String error;
  final VoidCallback onRetry;
  const _ErrorView({required this.error, required this.onRetry});

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.error_outline,
                size: 48, color: Theme.of(context).colorScheme.error),
            const SizedBox(height: 16),
            Text('加载失败', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            Text(error, textAlign: TextAlign.center,
                style: const TextStyle(fontSize: 13)),
            const SizedBox(height: 24),
            FilledButton.icon(
              onPressed: onRetry,
              icon: const Icon(Icons.refresh),
              label: const Text('重试'),
            ),
          ],
        ),
      ),
    );
  }
}

// ════════════════════════════════════════════════════════════════
//  通用对话框
// ════════════════════════════════════════════════════════════════
Future<void> _showTextEditDialog(
  BuildContext context, {
  required String title,
  required String label,
  required String initial,
  bool obscure = false,
  required Future<bool> Function(String) onSubmit,
}) async {
  final controller = TextEditingController(text: initial);
  final result = await showDialog<String>(
    context: context,
    builder: (ctx) => AlertDialog(
      title: Text(title),
      content: TextField(
        controller: controller,
        obscureText: obscure,
        decoration: InputDecoration(
          labelText: label,
          border: OutlineInputBorder(borderRadius: BorderRadius.circular(12)),
        ),
        autofocus: true,
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(ctx).pop(),
          child: const Text('取消'),
        ),
        FilledButton(
          onPressed: () {
            final v = controller.text.trim();
            if (v.isNotEmpty) Navigator.of(ctx).pop(v);
          },
          child: const Text('保存'),
        ),
      ],
    ),
  );
  controller.dispose();
  if (result != null && context.mounted) {
    final ok = await onSubmit(result);
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(ok ? '$title 已更新' : '保存失败'),
          behavior: SnackBarBehavior.floating,
        ),
      );
    }
  }
}

Future<void> _showNumberEditDialog(
  BuildContext context, {
  required String title,
  required num initial,
  required num min,
  num? max,
  bool isDouble = false,
  required Future<bool> Function(num) onSubmit,
}) async {
  final controller = TextEditingController(text: initial.toString());
  final result = await showDialog<num>(
    context: context,
    builder: (ctx) => AlertDialog(
      title: Text(title),
      content: TextField(
        controller: controller,
        keyboardType: isDouble
            ? const TextInputType.numberWithOptions(decimal: true)
            : TextInputType.number,
        decoration: InputDecoration(
          labelText: title,
          border: OutlineInputBorder(borderRadius: BorderRadius.circular(12)),
        ),
        autofocus: true,
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(ctx).pop(),
          child: const Text('取消'),
        ),
        FilledButton(
          onPressed: () {
            final v = isDouble
                ? double.tryParse(controller.text.trim())
                : int.tryParse(controller.text.trim());
            if (v != null && v >= min && (max == null || v <= max)) {
              Navigator.of(ctx).pop(v);
            }
          },
          child: const Text('保存'),
        ),
      ],
    ),
  );
  controller.dispose();
  if (result != null && context.mounted) {
    final ok = await onSubmit(result);
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(ok ? '$title 已更新' : '保存失败'),
          behavior: SnackBarBehavior.floating,
        ),
      );
    }
  }
}

Future<void> _showSliderDialog(
  BuildContext context, {
  required String title,
  required double initial,
  required double min,
  required double max,
  required int divisions,
  required Future<bool> Function(double) onSubmit,
}) async {
  double value = initial;
  final result = await showDialog<double>(
    context: context,
    builder: (ctx) => StatefulBuilder(
      builder: (ctx, setState) => AlertDialog(
        title: Text(title),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(value.toStringAsFixed(2),
                style: const TextStyle(fontSize: 32, fontFamily: 'monospace')),
            const SizedBox(height: 8),
            Slider(
              value: value,
              min: min,
              max: max,
              divisions: divisions,
              onChanged: (v) => setState(() => value = v),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(value),
            child: const Text('保存'),
          ),
        ],
      ),
    ),
  );
  if (result != null && context.mounted) {
    final ok = await onSubmit(result);
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(ok ? '$title 已更新' : '保存失败'),
          behavior: SnackBarBehavior.floating,
        ),
      );
    }
  }
}

Future<void> _showChoiceDialog(
  BuildContext context, {
  required String title,
  required Map<String, String> options,
  required String current,
  required Future<bool> Function(String) onSubmit,
}) async {
  final result = await showDialog<String>(
    context: context,
    builder: (ctx) => AlertDialog(
      title: Text(title),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        children: options.entries.map((e) {
          return RadioListTile<String>(
            title: Text(e.value),
            value: e.key,
            groupValue: current,
            onChanged: (v) => Navigator.of(ctx).pop(v),
          );
        }).toList(),
      ),
    ),
  );
  if (result != null && result != current && context.mounted) {
    final ok = await onSubmit(result);
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(ok ? '$title 已更新' : '保存失败'),
          behavior: SnackBarBehavior.floating,
        ),
      );
    }
  }
}

Future<void> _showInfoDialog(
  BuildContext context, {
  required String title,
  required List<String> lines,
}) {
  return showDialog(
    context: context,
    builder: (ctx) => AlertDialog(
      title: Text(title),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: lines
            .map((l) => Padding(
                  padding: const EdgeInsets.only(bottom: 6),
                  child: Text(l,
                      style: const TextStyle(
                          fontSize: 13, fontFamily: 'monospace')),
                ))
            .toList(),
      ),
      actions: [
        FilledButton(
          onPressed: () => Navigator.of(ctx).pop(),
          child: const Text('确定'),
        ),
      ],
    ),
  );
}

// ════════════════════════════════════════════════════════════════
//  模型管理相关对话框
// ════════════════════════════════════════════════════════════════

/// 显示模型测试结果对话框
void _showModelTestResult(BuildContext context, Map<String, dynamic>? result) {
  if (result == null) {
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('测试结果'),
        content: const Text('请求失败，请检查网络或服务器'),
        actions: [
          FilledButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('确定'),
          ),
        ],
      ),
    );
    return;
  }
  final ok = result['ok'] == true;
  final response = result['response'] as String? ?? '';
  final error = result['error'] as String? ?? '';
  final message = result['message'] as String? ?? '';
  showDialog(
    context: context,
    builder: (ctx) => AlertDialog(
      title: Row(
        children: [
          Icon(
            ok ? Icons.check_circle : Icons.error_outline,
            color: ok ? Colors.green : Colors.red,
          ),
          const SizedBox(width: 8),
          Text(ok ? '测试成功' : '测试失败'),
        ],
      ),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (message.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Text(message, style: const TextStyle(fontSize: 13)),
            ),
          if (ok && response.isNotEmpty) ...[
            const Text('响应内容：',
                style: TextStyle(fontSize: 12, fontWeight: FontWeight.w600)),
            const SizedBox(height: 4),
            Container(
              padding: const EdgeInsets.all(8),
              constraints: const BoxConstraints(maxHeight: 200),
              decoration: BoxDecoration(
                color: Theme.of(ctx).colorScheme.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(6),
              ),
              child: SingleChildScrollView(
                child: SelectableText(
                  response,
                  style: const TextStyle(
                    fontFamily: 'monospace',
                    fontSize: 12,
                  ),
                ),
              ),
            ),
          ] else if (!ok && error.isNotEmpty) ...[
            const Text('错误：',
                style: TextStyle(
                    fontSize: 12,
                    fontWeight: FontWeight.w600,
                    color: Colors.red)),
            const SizedBox(height: 4),
            Container(
              padding: const EdgeInsets.all(8),
              constraints: const BoxConstraints(maxHeight: 200),
              decoration: BoxDecoration(
                color: Colors.red.withOpacity(0.08),
                borderRadius: BorderRadius.circular(6),
              ),
              child: SingleChildScrollView(
                child: SelectableText(
                  error,
                  style: const TextStyle(
                    fontFamily: 'monospace',
                    fontSize: 12,
                    color: Colors.red,
                  ),
                ),
              ),
            ),
          ],
        ],
      ),
      actions: [
        FilledButton(
          onPressed: () => Navigator.of(ctx).pop(),
          child: const Text('确定'),
        ),
      ],
    ),
  );
}

/// 添加服务商对话框（2 级菜单：列出所有已知服务商）
Future<void> _showProviderManagementDialog(
  BuildContext context,
  ServerConfigNotifier notifier,
) async {
  // 显示加载对话框
  showDialog(
    context: context,
    barrierDismissible: false,
    builder: (ctx) => const Center(child: CircularProgressIndicator()),
  );

  final result = await SystemApi.listProviders();
  if (!context.mounted) return;
  Navigator.of(context).pop(); // 关闭加载对话框

  if (result == null) {
    ScaffoldMessenger.of(context).showSnackBar(
      const SnackBar(
        content: Text('获取服务商列表失败'),
        behavior: SnackBarBehavior.floating,
      ),
    );
    return;
  }

  final providers = (result['providers'] as List? ?? [])
      .whereType<Map<String, dynamic>>()
      .toList();
  final configured = _collectConfiguredProviders(notifier);

  // 问题1 修复：将客户端已配置但不在服务端 KNOWN_PROVIDERS 列表中的
  // 服务商也加入列表，确保所有有 key 的服务商都能显示（包括自定义服务商）。
  final knownNames = providers
      .map((p) => p['name'] as String? ?? '')
      .where((n) => n.isNotEmpty)
      .toSet();
  final baseUrls = notifier.providerBaseUrls;
  for (final name in configured) {
    if (!knownNames.contains(name)) {
      providers.add({
        'name': name,
        'base_url': baseUrls[name] ?? '',
        'has_key': true,
      });
    }
  }
  // 已配置的排在前面
  providers.sort((a, b) {
    final aName = a['name'] as String? ?? '';
    final bName = b['name'] as String? ?? '';
    final aConfigured = configured.contains(aName) ? 0 : 1;
    final bConfigured = configured.contains(bName) ? 0 : 1;
    if (aConfigured != bConfigured) return aConfigured - bConfigured;
    return aName.compareTo(bName);
  });

  await showDialog(
    context: context,
    builder: (ctx) => AlertDialog(
      title: const Text('添加服务商'),
      content: SizedBox(
        width: double.maxFinite,
        child: providers.isEmpty
            ? const Text('暂无可用服务商')
            : ListView.builder(
                shrinkWrap: true,
                itemCount: providers.length,
                itemBuilder: (ctx, i) {
                  final p = providers[i];
                  final name = p['name'] as String? ?? '';
                  final baseUrl = p['base_url'] as String? ?? '';
                  // 问题1 修复：hasKey 优先从客户端 state.config 判断
                  // （configuredProviders），而非 list_providers 的 has_key。
                  // 因为 LLM 热重载可能尚未完成，list_providers 返回的
                  // has_key 可能是过期值（False），导致已保存 key 的服务商
                  // 在对话框中显示为"未配置"。
                  final hasKey = configured.contains(name) || p['has_key'] == true;
                  final isConfigured = configured.contains(name);

                  return ListTile(
                    leading: Icon(
                      hasKey ? Icons.check_circle : Icons.circle_outlined,
                      color: hasKey ? Colors.green : null,
                      size: 20,
                    ),
                    title: Text(name,
                        style: const TextStyle(fontFamily: 'monospace')),
                    subtitle: Text(
                      baseUrl,
                      style: const TextStyle(
                          fontSize: 11, fontFamily: 'monospace'),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                    ),
                    trailing: isConfigured
                        ? const _InfoChip('已配置', color: Colors.green)
                        : (hasKey
                            ? const _InfoChip('已设 Key', color: Colors.green)
                            : null),
                    onTap: () {
                      Navigator.of(ctx).pop();
                      _showProviderConfigDialog(
                        context: context,
                        name: name,
                        baseUrl: baseUrl,
                        hasKey: hasKey,
                        notifier: notifier,
                      );
                    },
                  );
                },
              ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(ctx).pop(),
          child: const Text('关闭'),
        ),
      ],
    ),
  );
}

/// 问题2c：服务商模型缓存（内存级，app 运行期间持久）
/// 存储"测试全部"或详情页测试时拉取到的模型列表，避免重复请求。
/// 当用户重新打开服务商详情页时可直接展示缓存数据。
class ProviderModelCache {
  static final Map<String, _CachedModels> _cache = {};

  static void set(String provider,
      {required List<Map<String, dynamic>> free,
      required List<Map<String, dynamic>> paid}) {
    _cache[provider] = _CachedModels(
      free: free,
      paid: paid,
      fetchedAt: DateTime.now(),
    );
  }

  static _CachedModels? get(String provider) => _cache[provider];

  static void clear(String provider) => _cache.remove(provider);

  static void clearAll() => _cache.clear();

  /// 问题2 修复：获取所有缓存的模型（free + paid），合并为一个列表。
  /// 用于模型选择对话框，让新测试的服务商模型也能在选择器中出现。
  static List<Map<String, dynamic>> allCachedModels() {
    final all = <Map<String, dynamic>>[];
    for (final entry in _cache.values) {
      all.addAll(entry.free);
      all.addAll(entry.paid);
    }
    return all;
  }
}

class _CachedModels {
  final List<Map<String, dynamic>> free;
  final List<Map<String, dynamic>> paid;
  final DateTime fetchedAt;
  _CachedModels({
    required this.free,
    required this.paid,
    required this.fetchedAt,
  });
}

/// 提取当前已配置的服务商列表
/// 优先使用 config.llm.api_keys 中已配置 key 的服务商，
/// 再补充 available_models 中出现的 provider
Set<String> _collectConfiguredProviders(ServerConfigNotifier notifier) {
  final providers = <String>{};
  providers.addAll(notifier.configuredProviders);
  final primary = notifier.catalogPrimaryProvider;
  if (primary.isNotEmpty) providers.add(primary);
  final models = notifier.availableModels ?? [];
  for (final m in models) {
    if (m is Map<String, dynamic>) {
      final p = m['provider'] as String?;
      if (p != null && p.isNotEmpty) providers.add(p);
    }
  }
  return providers;
}

/// 服务商配置对话框：输入 API Key + base_url，测试连通后展示拉取到的模型列表供选择
/// 选中模型后可设为默认模型或加入 tier。
Future<void> _showProviderConfigDialog({
  required BuildContext context,
  required String name,
  required String baseUrl,
  required bool hasKey,
  required ServerConfigNotifier notifier,
}) async {
  await showDialog<void>(
    context: context,
    barrierDismissible: false,
    builder: (ctx) => _ProviderConfigDialog(
      providerName: name,
      defaultBaseUrl: baseUrl,
      hasKey: hasKey,
      notifier: notifier,
    ),
  );
}

/// 服务商详情面板（问题1 实现）
///
/// 显示：
/// - API Key（星号代替）+ API 地址
/// - 该服务商已添加的模型列表
/// - 测试按钮 → 拉取最新模型（免费/付费分类展示，带介绍）
/// - 可直接在分类模型列表点击「加入 tier」按钮
Future<void> _showProviderDetailSheet({
  required BuildContext context,
  required String name,
  required ServerConfigNotifier notifier,
}) async {
  await showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    shape: const RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
    ),
    builder: (ctx) => _ProviderDetailSheet(
      providerName: name,
      notifier: notifier,
    ),
  );
}

/// 服务商详情面板（StatefulWidget）
class _ProviderDetailSheet extends StatefulWidget {
  final String providerName;
  final ServerConfigNotifier notifier;

  const _ProviderDetailSheet({
    required this.providerName,
    required this.notifier,
  });

  @override
  State<_ProviderDetailSheet> createState() => _ProviderDetailSheetState();
}

class _ProviderDetailSheetState extends State<_ProviderDetailSheet> {
  bool _testing = false;
  String? _error;
  // 测试后拉取到的分类模型列表
  List<Map<String, dynamic>> _freeModels = [];
  List<Map<String, dynamic>> _paidModels = [];
  String _query = '';
  // 用户选中的模型 id（含 provider 前缀）
  final Set<String> _selectedToAdd = {};

  @override
  void initState() {
    super.initState();
    // 问题2c：打开详情页时，先从缓存加载之前拉取到的模型数据
    final cached = ProviderModelCache.get(widget.providerName);
    if (cached != null) {
      _freeModels = cached.free;
      _paidModels = cached.paid;
    }
  }

  bool get _hasKey =>
      widget.notifier.configuredProviders.contains(widget.providerName);
  String get _baseUrl =>
      widget.notifier.providerBaseUrls[widget.providerName] ?? '';
  bool get _isPrimary =>
      widget.notifier.catalogPrimaryProvider == widget.providerName;

  Future<void> _testAndFetch() async {
    setState(() {
      _testing = true;
      _error = null;
      _freeModels = [];
      _paidModels = [];
    });
    try {
      final result = await SystemApi.testProvider(
        provider: widget.providerName,
        // 留空 → 服务端使用已存储的 key 测试
        apiKey: '',
        baseUrl: _baseUrl,
      );
      if (!mounted) return;
      final ok = result?['ok'] == true;
      if (ok) {
        final free = ((result?['free_models'] as List?) ?? [])
            .whereType<Map<String, dynamic>>()
            .toList();
        final paid = ((result?['paid_models'] as List?) ?? [])
            .whereType<Map<String, dynamic>>()
            .toList();
        // 问题2c：缓存拉取到的模型数据，下次打开详情页时可直接展示
        ProviderModelCache.set(widget.providerName, free: free, paid: paid);
        setState(() {
          _testing = false;
          _freeModels = free;
          _paidModels = paid;
        });
      } else {
        setState(() {
          _testing = false;
          _error = result?['error']?.toString() ?? '连接失败';
        });
      }
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _testing = false;
        _error = e.toString();
      });
    }
  }

  /// 把选中的模型加入指定 tier 层
  Future<void> _addSelectedToTier(String tierName) async {
    if (_selectedToAdd.isEmpty) return;
    // 读取当前 tier 的模型列表
    final tiers = widget.notifier.tierData ?? {};
    final tierInfo = tiers[tierName] as Map<String, dynamic>?;
    final current = (tierInfo?['models'] as List? ?? [])
        .whereType<String>()
        .toList();
    // 合并去重，保留顺序（新增的追加到末尾）
    final merged = <String>[...current];
    for (final m in _selectedToAdd) {
      if (!merged.contains(m)) merged.add(m);
    }
    final ok = await widget.notifier.updateConfig({
      'llm': {
        'model_tiers': {tierName: merged}
      }
    });
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(ok
            ? '已将 ${_selectedToAdd.length} 个模型加入 $tierName 层'
            : widget.notifier.state.error ?? '保存失败'),
        behavior: SnackBarBehavior.floating,
      ),
    );
    if (ok) {
      setState(() {
        // 清空选中并刷新
        _selectedToAdd.clear();
      });
      await widget.notifier.loadModels();
    }
  }

  String _fullModelId(String raw) {
    return raw.contains('/') ? raw : '${widget.providerName}/$raw';
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final configuredModels = widget.notifier.modelsForProvider(widget.providerName);

    return DraggableScrollableSheet(
      initialChildSize: 0.85,
      minChildSize: 0.5,
      maxChildSize: 0.95,
      expand: false,
      builder: (ctx, scrollController) => Padding(
        padding: EdgeInsets.only(
          bottom: MediaQuery.of(ctx).viewInsets.bottom,
        ),
        child: Column(
          children: [
            // 拖动指示器
            Container(
              margin: const EdgeInsets.only(top: 8),
              width: 40,
              height: 4,
              decoration: BoxDecoration(
                color: theme.colorScheme.outline.withOpacity(0.4),
                borderRadius: BorderRadius.circular(2),
              ),
            ),
            // 标题栏
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 12, 12, 8),
              child: Row(
                children: [
                  Icon(Icons.cloud, color: theme.colorScheme.primary),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      widget.providerName,
                      style: theme.textTheme.titleMedium?.copyWith(
                        fontFamily: 'monospace',
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                  ),
                  if (_isPrimary)
                    Container(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 8, vertical: 2),
                      decoration: BoxDecoration(
                        color: theme.colorScheme.primary,
                        borderRadius: BorderRadius.circular(12),
                      ),
                      child: Text(
                        '主服务商',
                        style: TextStyle(
                          fontSize: 10,
                          color: theme.colorScheme.onPrimary,
                          fontWeight: FontWeight.bold,
                        ),
                      ),
                    )
                  else
                    TextButton.icon(
                      icon: const Icon(Icons.star_outline, size: 16),
                      label: const Text('设为主', style: TextStyle(fontSize: 12)),
                      onPressed: () async {
                        final ok = await widget.notifier.updateConfig({
                          'llm': {'primary_provider': widget.providerName}
                        });
                        if (!mounted) return;
                        ScaffoldMessenger.of(context).showSnackBar(
                          SnackBar(
                            content: Text(ok
                                ? '已设 ${widget.providerName} 为主服务商'
                                : widget.notifier.state.error ?? '设置失败'),
                            behavior: SnackBarBehavior.floating,
                          ),
                        );
                        // 问题1 修复：切换主服务商后用 loadConfig()
                        // 而非 loadModels()，确保 state.config 保留
                        // 所有 api_keys（包括新增服务商），避免消失。
                        if (ok) await widget.notifier.loadConfig();
                      },
                    ),
                  IconButton(
                    icon: const Icon(Icons.close),
                    onPressed: () => Navigator.of(ctx).pop(),
                  ),
                ],
              ),
            ),
            const Divider(height: 1),
            // 内容区
            Expanded(
              child: ListView(
                controller: scrollController,
                padding: const EdgeInsets.fromLTRB(20, 12, 20, 24),
                children: [
                  // ── API Key & 地址 ──────────────────────
                  _DetailRow(
                    label: 'API Key',
                    value: _hasKey ? '••••••••（已配置，留空保持不变）' : '未配置',
                    icon: Icons.key,
                    valueColor: _hasKey ? Colors.green : theme.colorScheme.error,
                  ),
                  const SizedBox(height: 8),
                  _DetailRow(
                    label: 'API 地址',
                    value: _baseUrl.isEmpty ? '使用服务商默认地址' : _baseUrl,
                    icon: Icons.link,
                  ),
                  const SizedBox(height: 8),
                  _DetailRow(
                    label: '已添加模型',
                    value: '${configuredModels.length} 个',
                    icon: Icons.model_training,
                  ),
                  const SizedBox(height: 16),

                  // ── 编辑按钮 ──────────────────────────
                  Row(
                    children: [
                      Expanded(
                        child: OutlinedButton.icon(
                          icon: const Icon(Icons.edit, size: 16),
                          label: const Text('编辑 Key / 地址'),
                          onPressed: () {
                            Navigator.of(ctx).pop();
                            _showProviderConfigDialog(
                              context: context,
                              name: widget.providerName,
                              baseUrl: _baseUrl,
                              hasKey: _hasKey,
                              notifier: widget.notifier,
                            );
                          },
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 16),

                  // ── 已添加模型列表 ──────────────────────
                  if (configuredModels.isNotEmpty) ...[
                    _SectionLabel(
                      icon: Icons.checklist,
                      text: '已添加模型（${configuredModels.length}）',
                    ),
                    const SizedBox(height: 8),
                    ...configuredModels.map((m) => _ProviderModelRow(
                          info: m,
                          showAddToTier: false,
                        )),
                    const SizedBox(height: 16),
                  ],

                  // ── 测试按钮 ──────────────────────────
                  FilledButton.icon(
                    onPressed: _testing ? null : _testAndFetch,
                    icon: _testing
                        ? const SizedBox(
                            width: 16,
                            height: 16,
                            child: CircularProgressIndicator(strokeWidth: 2))
                        : const Icon(Icons.wifi_find, size: 18),
                    label: Text(_testing ? '测试中...' : '测试连接并拉取最新模型'),
                  ),

                  // ── 错误提示 ──────────────────────────
                  if (_error != null) ...[
                    const SizedBox(height: 12),
                    Container(
                      width: double.infinity,
                      padding: const EdgeInsets.all(10),
                      decoration: BoxDecoration(
                        color: theme.colorScheme.errorContainer.withOpacity(0.3),
                        borderRadius: BorderRadius.circular(8),
                      ),
                      child: Text(
                        _error!,
                        style: TextStyle(
                          fontSize: 12,
                          color: theme.colorScheme.onErrorContainer,
                        ),
                      ),
                    ),
                  ],

                  // ── 拉取到的模型（分类展示） ────────────
                  if (_freeModels.isNotEmpty || _paidModels.isNotEmpty) ...[
                    const SizedBox(height: 16),
                    // 搜索框
                    TextField(
                      decoration: InputDecoration(
                        hintText: '搜索模型...',
                        prefixIcon: const Icon(Icons.search, size: 18),
                        isDense: true,
                        contentPadding: const EdgeInsets.symmetric(
                            horizontal: 12, vertical: 10),
                        border: OutlineInputBorder(
                            borderRadius: BorderRadius.circular(8)),
                      ),
                      onChanged: (v) => setState(() => _query = v),
                    ),
                    const SizedBox(height: 12),

                    // 选中模型操作栏
                    if (_selectedToAdd.isNotEmpty) ...[
                      Container(
                        padding: const EdgeInsets.symmetric(
                            horizontal: 12, vertical: 8),
                        margin: const EdgeInsets.only(bottom: 12),
                        decoration: BoxDecoration(
                          color: theme.colorScheme.primaryContainer
                              .withOpacity(0.4),
                          borderRadius: BorderRadius.circular(8),
                        ),
                        child: Row(
                          children: [
                            Text(
                              '已选 ${_selectedToAdd.length} 个模型，加入：',
                              style: theme.textTheme.labelMedium?.copyWith(
                                color: theme.colorScheme.primary,
                                fontWeight: FontWeight.w600,
                              ),
                            ),
                            const Spacer(),
                            _TierAddButton(
                              tier: 'trivial',
                              onPressed: () => _addSelectedToTier('trivial'),
                            ),
                            const SizedBox(width: 4),
                            _TierAddButton(
                              tier: 'simple',
                              onPressed: () => _addSelectedToTier('simple'),
                            ),
                            const SizedBox(width: 4),
                            _TierAddButton(
                              tier: 'complex',
                              onPressed: () => _addSelectedToTier('complex'),
                            ),
                            const SizedBox(width: 4),
                            _TierAddButton(
                              tier: 'expert',
                              onPressed: () => _addSelectedToTier('expert'),
                            ),
                          ],
                        ),
                      ),
                    ],

                    // 免费模型分类
                    if (_freeModels.isNotEmpty) ...[
                      _SectionLabel(
                        icon: Icons.bolt,
                        text: '免费模型（${_freeModels.length}）',
                        color: Colors.teal,
                      ),
                      const SizedBox(height: 8),
                      ..._filteredModels(_freeModels).map((m) =>
                          _ProviderModelRow(
                            info: m,
                            isSelected: _selectedToAdd.contains(_fullModelId(
                                (m['id'] as String?) ?? '')),
                            showAddToTier: true,
                            onToggle: () {
                              final id = _fullModelId(
                                  (m['id'] as String?) ?? '');
                              setState(() {
                                if (_selectedToAdd.contains(id)) {
                                  _selectedToAdd.remove(id);
                                } else {
                                  _selectedToAdd.add(id);
                                }
                              });
                            },
                          )),
                      const SizedBox(height: 16),
                    ],

                    // 付费模型分类
                    if (_paidModels.isNotEmpty) ...[
                      _SectionLabel(
                        icon: Icons.paid,
                        text: '付费模型（${_paidModels.length}）',
                        color: Colors.orange,
                      ),
                      const SizedBox(height: 8),
                      ..._filteredModels(_paidModels).map((m) =>
                          _ProviderModelRow(
                            info: m,
                            isSelected: _selectedToAdd.contains(_fullModelId(
                                (m['id'] as String?) ?? '')),
                            showAddToTier: true,
                            onToggle: () {
                              final id = _fullModelId(
                                  (m['id'] as String?) ?? '');
                              setState(() {
                                if (_selectedToAdd.contains(id)) {
                                  _selectedToAdd.remove(id);
                                } else {
                                  _selectedToAdd.add(id);
                                }
                              });
                            },
                          )),
                    ],
                  ],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  List<Map<String, dynamic>> _filteredModels(List<Map<String, dynamic>> src) {
    if (_query.isEmpty) return src;
    final q = _query.toLowerCase();
    return src.where((m) {
      final id = (m['id'] as String?) ?? '';
      final name = (m['name'] as String?) ?? '';
      final desc = (m['description'] as String?) ?? '';
      return id.toLowerCase().contains(q) ||
          name.toLowerCase().contains(q) ||
          desc.toLowerCase().contains(q);
    }).toList();
  }
}

/// 详情行（标签 + 值）
class _DetailRow extends StatelessWidget {
  final String label;
  final String value;
  final IconData icon;
  final Color? valueColor;

  const _DetailRow({
    required this.label,
    required this.value,
    required this.icon,
    this.valueColor,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Icon(icon, size: 14, color: theme.colorScheme.onSurfaceVariant),
        const SizedBox(width: 8),
        Text(
          '$label: ',
          style: theme.textTheme.bodySmall?.copyWith(
            color: theme.colorScheme.onSurfaceVariant,
            fontWeight: FontWeight.w600,
          ),
        ),
        Expanded(
          child: Text(
            value,
            style: theme.textTheme.bodySmall?.copyWith(
              fontFamily: 'monospace',
              color: valueColor,
            ),
          ),
        ),
      ],
    );
  }
}

/// 区块标签
class _SectionLabel extends StatelessWidget {
  final IconData icon;
  final String text;
  final Color? color;

  const _SectionLabel({
    required this.icon,
    required this.text,
    this.color,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final c = color ?? theme.colorScheme.primary;
    return Row(
      children: [
        Icon(icon, size: 14, color: c),
        const SizedBox(width: 6),
        Text(
          text,
          style: theme.textTheme.labelMedium?.copyWith(
            color: c,
            fontWeight: FontWeight.w600,
          ),
        ),
      ],
    );
  }
}

/// Tier 加入按钮
class _TierAddButton extends StatelessWidget {
  final String tier;
  final VoidCallback onPressed;

  const _TierAddButton({required this.tier, required this.onPressed});

  static const _labels = {
    'trivial': '极简',
    'simple': '简单',
    'complex': '复杂',
    'expert': '专家',
  };

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 28,
      child: FilledButton.tonal(
        onPressed: onPressed,
        style: FilledButton.styleFrom(
          padding: const EdgeInsets.symmetric(horizontal: 8),
          minimumSize: const Size(0, 28),
          tapTargetSize: MaterialTapTargetSize.shrinkWrap,
        ),
        child: Text(
          _labels[tier] ?? tier,
          style: const TextStyle(fontSize: 11),
        ),
      ),
    );
  }
}

/// 服务商模型行 — 显示模型 id + 详情 + 选择/加入 tier 按钮
class _ProviderModelRow extends StatelessWidget {
  final Map<String, dynamic> info;
  final bool isSelected;
  final bool showAddToTier;
  final VoidCallback? onToggle;

  const _ProviderModelRow({
    required this.info,
    this.isSelected = false,
    this.showAddToTier = false,
    this.onToggle,
  });

  /// 问题3：tier → (中文名, 推荐使用场景, 颜色)
  static const _tierMeta = {
    'trivial': ('极简', '简单问答/单轮对话', Colors.grey),
    'simple': ('简单', '日常任务/轻量生成', Colors.blue),
    'complex': ('复杂', '多步骤任务/工具调用', Colors.purple),
    'expert': ('专家', '深度推理/长文分析', Colors.deepOrange),
  };

  /// 问题3：免费模型速率限制（与服务端 _RATE_BUDGETS 对齐）
  static const _freeRateLimits = {
    'free': ('~20 req/min', '~200K tok/min'),
    'trial': ('~60 req/min', '~1M tok/min'),
  };

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final id = (info['id'] as String?) ?? '';
    final name = (info['name'] as String?) ?? '';
    final desc = (info['description'] as String?) ?? '';
    final isFree = info['is_free'] == true;
    final pricing = (info['pricing'] as Map?)?.cast<String, dynamic>();
    final caps = ((info['capabilities'] as List?) ?? [])
        .whereType<String>()
        .toList();
    final ctxLen = (info['context_length'] as num?)?.toInt() ?? 0;
    final maxOut = (info['max_output_length'] as num?)?.toInt() ?? 0;
    final supportsTools = info['supports_tools'] == true;
    final tier = (info['tier'] as String?) ?? '';
    final inMods = ((info['input_modalities'] as List?) ?? [])
        .whereType<String>()
        .toList();
    final outMods = ((info['output_modalities'] as List?) ?? [])
        .whereType<String>()
        .toList();
    final features = ((info['features'] as List?) ?? [])
        .whereType<String>()
        .toList();
    final quantization = (info['quantization'] as String?) ?? '';
    final tags = ((info['tags'] as List?) ?? [])
        .whereType<String>()
        .toList();

    // 问题3：价格详情 — 付费模型按字段展示（input/output/completion 分别显示）
    String priceText = '';
    if (isFree) {
      priceText = '免费';
    } else if (pricing != null && pricing.isNotEmpty) {
      final parts = <String>[];
      pricing.forEach((k, v) {
        final numV = (v is num) ? v : num.tryParse(v.toString());
        if (numV != null && numV > 0) {
          // 显示 $X / 1M tokens（行业惯例）
          final perM = numV * 1000000;
          final formatted = perM >= 1
              ? perM.toStringAsFixed(perM >= 100 ? 0 : 2)
              : perM.toStringAsFixed(4);
          parts.add('\$$formatted/$k');
        }
      });
      if (parts.isNotEmpty) priceText = parts.join(' · ');
    }

    // 问题3：免费模型限制参数
    String? freeLimit;
    if (isFree) {
      final hasTrialTag = tags.contains('trial');
      final limits = hasTrialTag ? _freeRateLimits['trial'] : _freeRateLimits['free'];
      if (limits != null) {
        freeLimit = '${limits.$1} / ${limits.$2}';
      }
    }

    // 问题3：上下文分类标签
    String? ctxTag;
    for (final t in tags) {
      if (t.endsWith('-context')) {
        ctxTag = t.replaceAll('-context', '');
        break;
      }
    }

    // 问题3：模态信息
    final modalityParts = <String>[];
    if (inMods.isNotEmpty) {
      modalityParts.add('输入:${inMods.join(",")}');
    }
    if (outMods.isNotEmpty && outMods != inMods) {
      modalityParts.add('输出:${outMods.join(",")}');
    }

    // 问题3：功能特性（合并 features + caps，去重）
    final allFeatures = <String>{...features, ...caps};
    final featureList = allFeatures
        .where((f) => !f.isEmpty)
        .take(6)
        .toList();

    // 问题3：tier 推荐信息
    final tierInfo = tier.isNotEmpty ? _tierMeta[tier] : null;

    return Card(
      margin: const EdgeInsets.only(bottom: 6),
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(8),
        side: BorderSide(
          color: isSelected
              ? theme.colorScheme.primary.withOpacity(0.5)
              : theme.colorScheme.outlineVariant.withOpacity(0.3),
        ),
      ),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (showAddToTier)
              Padding(
                padding: const EdgeInsets.only(top: 2, right: 8),
                child: InkWell(
                  onTap: onToggle,
                  child: Icon(
                    isSelected
                        ? Icons.check_circle
                        : Icons.add_circle_outline,
                    size: 20,
                    color: isSelected
                        ? theme.colorScheme.primary
                        : theme.colorScheme.onSurfaceVariant,
                  ),
                ),
              )
            else
              Padding(
                padding: const EdgeInsets.only(top: 2, right: 8),
                child: Icon(
                  isFree ? Icons.bolt : Icons.paid,
                  size: 16,
                  color: isFree ? Colors.teal : Colors.orange,
                ),
              ),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  // 第一行：模型 ID + 免费/付费标签 + 推荐路由层标签
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          id,
                          style: const TextStyle(
                            fontFamily: 'monospace',
                            fontSize: 13,
                            fontWeight: FontWeight.w600,
                          ),
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                      // 问题3：推荐路由层标签
                      if (tierInfo != null) ...[
                        Container(
                          padding: const EdgeInsets.symmetric(
                              horizontal: 5, vertical: 1),
                          decoration: BoxDecoration(
                            color: tierInfo.$3.withOpacity(0.15),
                            borderRadius: BorderRadius.circular(3),
                            border: Border.all(
                              color: tierInfo.$3.withOpacity(0.4),
                              width: 0.5,
                            ),
                          ),
                          child: Text(
                            '推荐:${tierInfo.$1}',
                            style: TextStyle(
                              fontSize: 9,
                              color: tierInfo.$3,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                        ),
                        const SizedBox(width: 4),
                      ],
                      Container(
                        padding: const EdgeInsets.symmetric(
                            horizontal: 5, vertical: 1),
                        decoration: BoxDecoration(
                          color: (isFree ? Colors.teal : Colors.orange)
                              .withOpacity(0.15),
                          borderRadius: BorderRadius.circular(3),
                        ),
                        child: Text(
                          isFree ? '免费' : '付费',
                          style: TextStyle(
                            fontSize: 9,
                            color: isFree ? Colors.teal : Colors.orange,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                      ),
                    ],
                  ),
                  // 第二行：name（如果有）
                  if (name.isNotEmpty && name != id) ...[
                    const SizedBox(height: 2),
                    Text(
                      name,
                      style: TextStyle(
                        fontSize: 11,
                        color: theme.colorScheme.onSurfaceVariant,
                      ),
                    ),
                  ],
                  // 第三行：价格 + 免费限制
                  if (priceText.isNotEmpty || freeLimit != null) ...[
                    const SizedBox(height: 4),
                    Wrap(
                      spacing: 6,
                      runSpacing: 3,
                      children: [
                        if (priceText.isNotEmpty)
                          Text(
                            priceText,
                            style: TextStyle(
                              fontSize: 10,
                              color: isFree
                                  ? Colors.teal
                                  : Colors.orange.shade700,
                              fontWeight: FontWeight.w500,
                            ),
                          ),
                        if (freeLimit != null)
                          Text(
                            '限速: $freeLimit',
                            style: TextStyle(
                              fontSize: 9,
                              color: theme.colorScheme.onSurfaceVariant,
                              fontStyle: FontStyle.italic,
                            ),
                          ),
                      ],
                    ),
                  ],
                  // 第四行：详情标签 — 上下文/输出/模态/功能/量化
                  const SizedBox(height: 4),
                  Wrap(
                    spacing: 6,
                    runSpacing: 4,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      if (ctxLen > 0)
                        _MiniInfo(
                            '上下文 ${_fmtCtx(ctxLen)}${ctxTag != null ? " ($ctxTag)" : ""}'),
                      if (maxOut > 0)
                        _MiniInfo('输出 ${_fmtCtx(maxOut)}'),
                      if (supportsTools)
                        const _MiniInfo('工具调用', highlight: true),
                      ...modalityParts.map((m) => _MiniInfo(m)),
                      ...featureList.map((f) => _CapabilityChip(cap: f)),
                      if (quantization.isNotEmpty)
                        _MiniInfo('量化:$quantization'),
                    ],
                  ),
                  // 第五行：描述
                  if (desc.isNotEmpty) ...[
                    const SizedBox(height: 4),
                    Text(
                      desc,
                      style: TextStyle(
                        fontSize: 11,
                        color: theme.colorScheme.onSurfaceVariant,
                        fontStyle: FontStyle.italic,
                      ),
                      maxLines: 3,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ],
                  // 第六行：推荐使用场景（问题3）
                  if (tierInfo != null) ...[
                    const SizedBox(height: 4),
                    Container(
                      width: double.infinity,
                      padding: const EdgeInsets.symmetric(
                          horizontal: 6, vertical: 3),
                      decoration: BoxDecoration(
                        color: tierInfo.$3.withOpacity(0.08),
                        borderRadius: BorderRadius.circular(4),
                      ),
                      child: Text(
                        '适合: ${tierInfo.$2}',
                        style: TextStyle(
                          fontSize: 10,
                          color: tierInfo.$3,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                    ),
                  ],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  static String _fmtCtx(int n) {
    if (n >= 1000000) return '${(n / 1000000).toStringAsFixed(1)}M';
    if (n >= 1000) return '${(n / 1000).toStringAsFixed(0)}K';
    return n.toString();
  }
}

/// 服务商配置对话框（StatefulWidget）
/// 流程：输入 Key → 测试连通 → 展示模型列表 → 选择模型 → 保存
class _ProviderConfigDialog extends StatefulWidget {
  final String providerName;
  final String defaultBaseUrl;
  final bool hasKey;
  final ServerConfigNotifier notifier;

  const _ProviderConfigDialog({
    required this.providerName,
    required this.defaultBaseUrl,
    required this.hasKey,
    required this.notifier,
  });

  @override
  State<_ProviderConfigDialog> createState() => _ProviderConfigDialogState();
}

class _ProviderConfigDialogState extends State<_ProviderConfigDialog> {
  final _keyController = TextEditingController();
  final _urlController = TextEditingController();
  bool _obscure = true;
  bool _testing = false;
  bool _saving = false;
  String? _error;
  // 测试成功后拉取到的模型列表
  List<String> _fetchedModels = [];
  String _modelQuery = '';
  // 用户选中的模型（带 provider 前缀，如 "openai/gpt-4o"）
  final Set<String> _selectedModels = {};

  @override
  void initState() {
    super.initState();
    _urlController.text = widget.defaultBaseUrl;
  }

  @override
  void dispose() {
    _keyController.dispose();
    _urlController.dispose();
    super.dispose();
  }

  Future<void> _testConnection() async {
    final key = _keyController.text.trim();
    final url = _urlController.text.trim();
    // 已配置 Key 时允许留空（服务端会使用已存储的 Key 测试）
    if (key.isEmpty && !widget.hasKey) {
      setState(() => _error = '请输入 API Key');
      return;
    }
    setState(() {
      _testing = true;
      _error = null;
      _fetchedModels = [];
    });
    try {
      final result = await SystemApi.testProvider(
        provider: widget.providerName,
        apiKey: key,
        baseUrl: url,
      );
      if (!mounted) return;
      final ok = result?['ok'] == true;
      if (ok) {
        final models = (result?['models'] as List? ?? [])
            .whereType<String>()
            .where((m) => m.isNotEmpty)
            .toList();
        setState(() {
          _testing = false;
          _fetchedModels = models;
          _error = null;
        });
      } else {
        setState(() {
          _testing = false;
          _error = result?['error']?.toString() ?? '连接失败';
        });
      }
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _testing = false;
        _error = e.toString();
      });
    }
  }

  Future<void> _saveAndExit() async {
    final key = _keyController.text.trim();
    // 已配置 Key 时允许留空（不修改原 Key）；新配置时必须输入
    if (key.isEmpty && !widget.hasKey) {
      setState(() => _error = '请输入 API Key');
      return;
    }
    setState(() {
      _saving = true;
      _error = null;
    });

    // 构建更新体 — 只包含实际要修改的字段
    final llmUpdates = <String, dynamic>{};

    // 1. 保存 API Key（仅当用户输入了新 Key 时才发送；
    //    留空时跳过，服务端保留原 Key）
    if (key.isNotEmpty) {
      llmUpdates['api_keys'] = {widget.providerName: key};
    }
    // 2. 如果用户选了模型，将第一个设为默认模型（provider/model 格式）
    if (_selectedModels.isNotEmpty) {
      final firstModel = _selectedModels.first;
      // 确保带 provider 前缀
      final fullId = firstModel.contains('/')
          ? firstModel
          : '${widget.providerName}/$firstModel';
      llmUpdates['primary_model'] = fullId;
      llmUpdates['primary_provider'] = widget.providerName;
    }
    // 3. base_url 保存
    // 问题2c 修复：新添加的服务商如果没有已保存的 base_url，即使 URL
    // 等于默认值也要保存，这样详情页能正确显示 API 地址。
    final url = _urlController.text.trim();
    final existingUrl = widget.notifier.providerBaseUrls[widget.providerName] ?? '';
    if (url.isNotEmpty && (url != existingUrl || existingUrl.isEmpty)) {
      llmUpdates['base_urls'] = {widget.providerName: url};
    }

    if (llmUpdates.isEmpty) {
      // 没有任何修改
      Navigator.of(context).pop();
      return;
    }

    final updates = <String, dynamic>{'llm': llmUpdates};

    final ok = await widget.notifier.updateConfig(updates);
    if (!mounted) return;
    setState(() => _saving = false);
    if (ok) {
      // 问题1 修复：保存后调用 loadConfig()（同时刷新 config + models），
      // 而非仅 loadModels()。确保 state.config 立即包含新增服务商的
      // api_keys（"***"），使 configuredProviders 正确返回新服务商，
      // 避免切换主服务商后新增服务商从列表中消失。
      await widget.notifier.loadConfig();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(_selectedModels.isEmpty
              ? '${widget.providerName} 配置已保存'
              : '${widget.providerName} 已配置，默认模型已更新'),
          behavior: SnackBarBehavior.floating,
        ),
      );
      Navigator.of(context).pop();
    } else {
      setState(() => _error = widget.notifier.state.error ?? '保存失败');
    }
  }

  String _fullModelId(String m) {
    return m.contains('/') ? m : '${widget.providerName}/$m';
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final filteredModels = _fetchedModels.where((m) {
      if (_modelQuery.isEmpty) return true;
      return m.toLowerCase().contains(_modelQuery.toLowerCase());
    }).toList();

    return AlertDialog(
      title: Row(
        children: [
          Icon(Icons.cloud, size: 20, color: theme.colorScheme.primary),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              '配置 ${widget.providerName}',
              style: const TextStyle(fontFamily: 'monospace'),
              overflow: TextOverflow.ellipsis,
            ),
          ),
        ],
      ),
      content: SizedBox(
        width: double.maxFinite,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (widget.hasKey)
                Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: _InfoChip('已配置 Key（留空保持不变）',
                      icon: Icons.check, color: Colors.green),
                ),
              // API Key 输入
              // 当服务商已配置 Key 时，留空表示不修改原 Key（服务端会跳过 "***" 哨兵）
              TextField(
                controller: _keyController,
                obscureText: _obscure,
                decoration: InputDecoration(
                  labelText: 'API Key',
                  hintText: widget.hasKey ? '留空保持不变' : '输入 API Key',
                  prefixIcon: const Icon(Icons.key, size: 18),
                  suffixIcon: IconButton(
                    icon: Icon(
                        _obscure ? Icons.visibility_off : Icons.visibility,
                        size: 18),
                    onPressed: () => setState(() => _obscure = !_obscure),
                  ),
                  border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(12)),
                  isDense: true,
                ),
              ),
              const SizedBox(height: 12),
              // Base URL 输入
              TextField(
                controller: _urlController,
                keyboardType: TextInputType.url,
                decoration: InputDecoration(
                  labelText: 'Base URL',
                  hintText: widget.defaultBaseUrl.isEmpty
                      ? '使用默认地址'
                      : widget.defaultBaseUrl,
                  prefixIcon: const Icon(Icons.link, size: 18),
                  border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(12)),
                  isDense: true,
                ),
              ),
              const SizedBox(height: 12),
              // 测试按钮
              SizedBox(
                width: double.infinity,
                child: FilledButton.tonalIcon(
                  onPressed: _testing ? null : _testConnection,
                  icon: _testing
                      ? const SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(strokeWidth: 2))
                      : const Icon(Icons.wifi_find, size: 16),
                  label: Text(_testing ? '测试中...' : '测试连接并拉取模型'),
                ),
              ),
              // 错误提示
              if (_error != null)
                Padding(
                  padding: const EdgeInsets.only(top: 10),
                  child: Container(
                    width: double.infinity,
                    padding: const EdgeInsets.all(8),
                    decoration: BoxDecoration(
                      color: theme.colorScheme.errorContainer.withOpacity(0.3),
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: Text(
                      _error!,
                      style: TextStyle(
                        fontSize: 12,
                        color: theme.colorScheme.onErrorContainer,
                      ),
                    ),
                  ),
                ),
              // 模型列表（测试成功后展示）
              if (_fetchedModels.isNotEmpty) ...[
                const SizedBox(height: 14),
                Row(
                  children: [
                    Icon(Icons.list_alt,
                        size: 14, color: theme.colorScheme.primary),
                    const SizedBox(width: 6),
                    Text(
                      '可用模型 (${_fetchedModels.length})',
                      style: theme.textTheme.labelMedium?.copyWith(
                        color: theme.colorScheme.primary,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    const Spacer(),
                    if (_selectedModels.isNotEmpty)
                      Text(
                        '已选 ${_selectedModels.length}',
                        style: theme.textTheme.labelSmall?.copyWith(
                          color: theme.colorScheme.primary,
                        ),
                      ),
                  ],
                ),
                const SizedBox(height: 8),
                // 搜索框
                TextField(
                  decoration: InputDecoration(
                    hintText: '搜索模型...',
                    prefixIcon: const Icon(Icons.search, size: 16),
                    isDense: true,
                    contentPadding: const EdgeInsets.symmetric(
                        horizontal: 10, vertical: 8),
                    border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(8)),
                  ),
                  onChanged: (v) => setState(() => _modelQuery = v),
                ),
                const SizedBox(height: 8),
                // 模型列表（可勾选）
                ConstrainedBox(
                  constraints: const BoxConstraints(maxHeight: 260),
                  child: ListView.builder(
                    shrinkWrap: true,
                    itemCount: filteredModels.length,
                    itemBuilder: (ctx, i) {
                      final m = filteredModels[i];
                      final fullId = _fullModelId(m);
                      final isSelected = _selectedModels.contains(fullId);
                      return CheckboxListTile(
                        value: isSelected,
                        dense: true,
                        contentPadding: const EdgeInsets.symmetric(
                            horizontal: 4, vertical: 0),
                        title: Text(
                          m,
                          style: const TextStyle(
                            fontFamily: 'monospace',
                            fontSize: 12,
                          ),
                          overflow: TextOverflow.ellipsis,
                        ),
                        subtitle: Text(
                          '${widget.providerName}/$m',
                          style: TextStyle(
                            fontSize: 10,
                            color: theme.colorScheme.outline,
                          ),
                        ),
                        onChanged: (v) {
                          setState(() {
                            if (v == true) {
                              _selectedModels.add(fullId);
                            } else {
                              _selectedModels.remove(fullId);
                            }
                          });
                        },
                      );
                    },
                  ),
                ),
                if (_selectedModels.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 6),
                    child: Text(
                      '提示：保存后，第一个选中的模型将设为默认模型',
                      style: theme.textTheme.labelSmall?.copyWith(
                        color: theme.colorScheme.outline,
                        fontStyle: FontStyle.italic,
                      ),
                    ),
                  ),
              ],
            ],
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: (_testing || _saving)
              ? null
              : () => Navigator.of(context).pop(),
          child: const Text('取消'),
        ),
        FilledButton(
          onPressed: (_testing || _saving) ? null : _saveAndExit,
          child: _saving
              ? const SizedBox(
                  width: 14,
                  height: 14,
                  child: CircularProgressIndicator(strokeWidth: 2))
              : const Text('保存'),
        ),
      ],
    );
  }
}

/// 模型选择对话框（带搜索 + tier/能力标签）
/// 从 available_models 列表中选择，无可用模型时回退到文本输入
Future<void> _showModelSelectionDialog(
  BuildContext context, {
  required String title,
  required String current,
  required ServerConfigNotifier notifier,
  required Future<bool> Function(String) onSubmit,
}) async {
  // 问题2 修复：合并 available_models（来自服务端 /api/models）和
  // ProviderModelCache 中缓存的服务商测试模型，确保新测试的服务商
  // 模型也能在选择器中出现，无需等待服务端 catalog 刷新。
  final serverModels = (notifier.availableModels ?? [])
      .whereType<Map<String, dynamic>>()
      .toList();
  final cachedModels = ProviderModelCache.allCachedModels();
  // 合并去重（按 model id）
  final seenIds = <String>{};
  final models = <Map<String, dynamic>>[];
  for (final m in [...serverModels, ...cachedModels]) {
    final id = (m['id'] as String?) ?? '';
    if (id.isNotEmpty && !seenIds.contains(id)) {
      seenIds.add(id);
      models.add(m);
    }
  }

  // 问题6 修复：无可用模型时不回退到文本编辑（用户不应手动修改模型名称），
  // 而是提示用户先测试服务商拉取模型列表。
  if (models.isEmpty) {
    await showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(title),
        content: const Text(
          '暂无可用模型列表。\n\n请先在「服务商」区域点击服务商名称，'
          '在详情页中点击「测试」按钮拉取最新模型列表，'
          '之后再回来选择模型。',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: const Text('知道了'),
          ),
        ],
      ),
    );
    return;
  }

  String query = '';
  String? selected;

  final result = await showDialog<String>(
    context: context,
    builder: (ctx) => StatefulBuilder(
      builder: (ctx, setState) {
        final filtered = models.where((m) {
          if (query.isEmpty) return true;
          final q = query.toLowerCase();
          final id = (m['id'] as String?) ?? '';
          final name = (m['name'] as String?) ?? '';
          final provider = (m['provider'] as String?) ?? '';
          return id.toLowerCase().contains(q) ||
              name.toLowerCase().contains(q) ||
              provider.toLowerCase().contains(q);
        }).toList();

        return AlertDialog(
          title: Row(
            children: [
              Text(title),
              const Spacer(),
              Text(
                '${filtered.length}/${models.length}',
                style: TextStyle(
                  fontSize: 12,
                  color: Theme.of(ctx).colorScheme.onSurfaceVariant,
                ),
              ),
            ],
          ),
          content: SizedBox(
            width: double.maxFinite,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  decoration: InputDecoration(
                    hintText: '搜索模型 ID / 名称 / 服务商...',
                    prefixIcon: const Icon(Icons.search, size: 18),
                    isDense: true,
                    contentPadding: const EdgeInsets.symmetric(
                        horizontal: 12, vertical: 10),
                    border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(8)),
                  ),
                  onChanged: (v) => setState(() => query = v),
                ),
                const SizedBox(height: 12),
                ConstrainedBox(
                  constraints: const BoxConstraints(maxHeight: 400),
                  child: ListView.builder(
                    shrinkWrap: true,
                    itemCount: filtered.length,
                    itemBuilder: (ctx, i) {
                      final m = filtered[i];
                      final id = (m['id'] as String?) ?? '';
                      final tier = (m['tier'] as String?) ?? '';
                      final caps = ((m['capabilities'] as List?) ?? [])
                          .whereType<String>()
                          .toList();
                      final isFree = m['is_free'] == true;
                      final ctxLen =
                          (m['context_length'] as num?)?.toInt() ?? 0;
                      final provider =
                          (m['provider'] as String?) ?? '';
                      final isCurrent = id == current;

                      return RadioListTile<String>(
                        value: id,
                        groupValue: selected ?? current,
                        onChanged: (v) => setState(() => selected = v),
                        dense: true,
                        contentPadding: const EdgeInsets.symmetric(
                            horizontal: 8, vertical: 0),
                        title: Row(
                          children: [
                            Expanded(
                              child: Text(
                                id,
                                style: const TextStyle(
                                  fontFamily: 'monospace',
                                  fontSize: 13,
                                ),
                                overflow: TextOverflow.ellipsis,
                              ),
                            ),
                            // 问题3：免费/付费标签
                            Container(
                              padding: const EdgeInsets.symmetric(
                                  horizontal: 4, vertical: 1),
                              decoration: BoxDecoration(
                                color: (isFree ? Colors.teal : Colors.orange)
                                    .withOpacity(0.15),
                                borderRadius: BorderRadius.circular(3),
                              ),
                              child: Text(
                                isFree ? '免费' : '付费',
                                style: TextStyle(
                                  fontSize: 9,
                                  color: isFree
                                      ? Colors.teal
                                      : Colors.orange,
                                  fontWeight: FontWeight.w600,
                                ),
                              ),
                            ),
                            if (isCurrent) ...[
                              const SizedBox(width: 4),
                              const _InfoChip('当前', color: Colors.green),
                            ],
                          ],
                        ),
                        subtitle: tier.isNotEmpty ||
                                caps.isNotEmpty ||
                                ctxLen > 0 ||
                                provider.isNotEmpty
                            ? Padding(
                                padding: const EdgeInsets.only(top: 4),
                                child: Wrap(
                                  spacing: 4,
                                  runSpacing: 4,
                                  crossAxisAlignment:
                                      WrapCrossAlignment.center,
                                  children: [
                                    if (provider.isNotEmpty)
                                      _MiniInfo(provider),
                                    if (tier.isNotEmpty)
                                      _TierBadge(tier: tier),
                                    if (ctxLen > 0)
                                      _MiniInfo(
                                          'ctx ${_ProviderModelRow._fmtCtx(ctxLen)}'),
                                    ...caps.take(4)
                                        .map((c) => _CapabilityChip(cap: c)),
                                  ],
                                ),
                              )
                            : null,
                      );
                    },
                  ),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(),
              child: const Text('取消'),
            ),
            FilledButton(
              onPressed: selected != null && selected != current
                  ? () => Navigator.of(ctx).pop(selected)
                  : null,
              child: const Text('选择'),
            ),
          ],
        );
      },
    ),
  );

  if (result != null && result != current && context.mounted) {
    final ok = await onSubmit(result);
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(ok ? '$title 已更新' : '保存失败'),
          behavior: SnackBarBehavior.floating,
        ),
      );
    }
    if (ok) await notifier.loadModels();
  }
}

/// Tier 层模型选择对话框 — 勾选/取消该层的模型，并支持拖拽排序
///
/// 问题4 修复：
/// - 字段路径从 router.tier_models 改为 llm.model_tiers（与服务端对齐）
/// - 使用 List 保留用户设置的调用顺序，路由时按此顺序回退
/// - 添加 ReorderableListView 支持长按拖动调整顺序
/// - 显示模型详情（免费/付费、价格、能力、上下文长度）
Future<void> _showTierModelsDialog(
  BuildContext context, {
  required String tierName,
  required List<String> currentModels,
  required ServerConfigNotifier notifier,
}) async {
  // 候选模型 = 当前 tier 已有模型 + 所有 available_models + 缓存模型（去重，保留顺序）
  // 问题2 修复：合并 ProviderModelCache 中的缓存模型，让新测试的服务商
  // 模型也能在 4 层路由模型选择中出现。
  final serverModels = (notifier.availableModels ?? [])
      .whereType<Map<String, dynamic>>()
      .toList();
  final cachedModels = ProviderModelCache.allCachedModels();
  final availableModels = <Map<String, dynamic>>[];
  final _seenTierIds = <String>{};
  for (final m in [...serverModels, ...cachedModels]) {
    final id = (m['id'] as String?) ?? '';
    if (id.isNotEmpty && !_seenTierIds.contains(id)) {
      _seenTierIds.add(id);
      availableModels.add(m);
    }
  }

  // 已选模型用 List 保留顺序（路由调用顺序）
  // 初始顺序：先按 currentModels 原始顺序，再补充其他已选的
  final orderedSelected = <String>[];
  for (final m in currentModels) {
    if (!orderedSelected.contains(m)) orderedSelected.add(m);
  }
  // 候选集（含未选）
  final candidateIds = <String>[...orderedSelected];
  for (final m in availableModels) {
    final id = (m['id'] as String?) ?? '';
    if (id.isNotEmpty && !candidateIds.contains(id)) candidateIds.add(id);
  }

  // 构建 id -> info 映射
  final modelInfoMap = <String, Map<String, dynamic>>{};
  for (final m in availableModels) {
    final id = (m['id'] as String?) ?? '';
    if (id.isNotEmpty) modelInfoMap[id] = m;
  }

  // 待选（未勾选）模型列表
  var unselected = candidateIds.where((id) => !orderedSelected.contains(id)).toList()
    ..sort();

  final result = await showDialog<bool>(
    context: context,
    builder: (ctx) => StatefulBuilder(
      builder: (ctx, setState) {
        final theme = Theme.of(ctx);
        return AlertDialog(
          title: Row(
            children: [
              Icon(Icons.layers,
                  size: 20, color: theme.colorScheme.primary),
              const SizedBox(width: 8),
              Expanded(
                child: Text('$tierName 层模型（已选 ${orderedSelected.length}）'),
              ),
            ],
          ),
          content: SizedBox(
            width: double.maxFinite,
            child: candidateIds.isEmpty
                ? const Text('暂无可用模型')
                : ConstrainedBox(
                    constraints: const BoxConstraints(maxHeight: 520),
                    child: ListView(
                      shrinkWrap: true,
                      children: [
                        // ── 已选模型（可拖拽排序） ──────────────
                        if (orderedSelected.isNotEmpty) ...[
                          Padding(
                            padding: const EdgeInsets.only(bottom: 6),
                            child: Row(
                              children: [
                                Icon(Icons.drag_indicator,
                                    size: 14, color: theme.colorScheme.primary),
                                const SizedBox(width: 4),
                                Text(
                                  '已选（长按拖动调整调用顺序）',
                                  style: theme.textTheme.labelSmall?.copyWith(
                                    color: theme.colorScheme.primary,
                                    fontWeight: FontWeight.w600,
                                  ),
                                ),
                              ],
                            ),
                          ),
                          // ReorderableListView 需要明确高度，外包 Container
                          Container(
                            decoration: BoxDecoration(
                              border: Border.all(
                                color: theme.colorScheme.primary.withOpacity(0.3),
                              ),
                              borderRadius: BorderRadius.circular(8),
                            ),
                            child: ReorderableListView(
                              shrinkWrap: true,
                              physics: const NeverScrollableScrollPhysics(),
                              buildDefaultDragHandles: false,
                              onReorder: (oldIdx, newIdx) {
                                setState(() {
                                  if (newIdx > oldIdx) newIdx -= 1;
                                  final item = orderedSelected.removeAt(oldIdx);
                                  orderedSelected.insert(newIdx, item);
                                });
                              },
                              children: List.generate(orderedSelected.length, (i) {
                                final id = orderedSelected[i];
                                return _TierModelTile(
                                  key: ValueKey('sel_$id'),
                                  id: id,
                                  info: modelInfoMap[id],
                                  index: i,
                                  isSelected: true,
                                  onToggle: () => setState(() {
                                    orderedSelected.remove(id);
                                    unselected.add(id);
                                    unselected.sort();
                                  }),
                                );
                              }),
                            ),
                          ),
                          const SizedBox(height: 12),
                        ],
                        // ── 未选模型 ──────────────────────────
                        if (unselected.isNotEmpty) ...[
                          Padding(
                            padding: const EdgeInsets.only(bottom: 6),
                            child: Text(
                              '可选模型（${unselected.length}）',
                              style: theme.textTheme.labelSmall?.copyWith(
                                color: theme.colorScheme.onSurfaceVariant,
                                fontWeight: FontWeight.w600,
                              ),
                            ),
                          ),
                          Container(
                            decoration: BoxDecoration(
                              border: Border.all(
                                color: theme.colorScheme.outlineVariant
                                    .withOpacity(0.5),
                              ),
                              borderRadius: BorderRadius.circular(8),
                            ),
                            child: Column(
                              children: unselected.map((id) {
                                return _TierModelTile(
                                  key: ValueKey('unsel_$id'),
                                  id: id,
                                  info: modelInfoMap[id],
                                  isSelected: false,
                                  onToggle: () => setState(() {
                                    unselected.remove(id);
                                    orderedSelected.add(id);
                                  }),
                                );
                              }).toList(),
                            ),
                          ),
                        ],
                      ],
                    ),
                  ),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(ctx).pop(false),
              child: const Text('取消'),
            ),
            FilledButton(
              onPressed: orderedSelected.isEmpty
                  ? null
                  : () => Navigator.of(ctx).pop(true),
              child: const Text('保存'),
            ),
          ],
        );
      },
    ),
  );

  if (result == true && context.mounted) {
    // 修复：字段路径从 router.tier_models 改为 llm.model_tiers，
    // 与服务端 models/__init__.py:238 的 llm_cfg.get("model_tiers") 对齐。
    // 之前写 router.tier_models 服务端根本不读，导致勾选/移除无效。
    // 保留 List 顺序（不 sort），让路由按用户设置的顺序调用模型。
    final ok = await notifier.updateConfig({
      'llm': {
        'model_tiers': {
          tierName: orderedSelected.toList(),
        }
      }
    });
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(ok
              ? '$tierName 层模型已更新（${orderedSelected.length} 个，按顺序调用）'
              : notifier.state.error ?? '保存失败'),
          behavior: SnackBarBehavior.floating,
        ),
      );
    }
    if (ok) await notifier.loadModels();
  }
}

/// Tier 模型条目 — 显示模型 ID + 详情（免费/付费、价格、能力）+ 勾选/移除按钮
class _TierModelTile extends StatelessWidget {
  final String id;
  final Map<String, dynamic>? info;
  final int? index;
  final bool isSelected;
  final VoidCallback onToggle;

  const _TierModelTile({
    super.key,
    required this.id,
    this.info,
    this.index,
    required this.isSelected,
    required this.onToggle,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isFree = info?['is_free'] == true;
    final pricing = (info?['pricing'] as Map?)?.cast<String, dynamic>();
    final desc = (info?['description'] as String?) ?? '';
    final caps = ((info?['capabilities'] as List?) ?? [])
        .whereType<String>()
        .toList();
    final ctxLen = (info?['context_length'] as num?)?.toInt() ?? 0;
    final supportsTools = info?['supports_tools'] == true;

    // 价格描述
    String priceText = '';
    if (isFree) {
      priceText = '免费';
    } else if (pricing != null && pricing.isNotEmpty) {
      final parts = <String>[];
      pricing.forEach((k, v) {
        final numV = (v is num) ? v : num.tryParse(v.toString());
        if (numV != null && numV > 0) {
          // 价格通常以 per-token USD 表示，转成 per-1K-tokens 更可读
          parts.add('$k: \$${(numV * 1000).toStringAsFixed(4)}/1K');
        }
      });
      if (parts.isNotEmpty) priceText = parts.join(' · ');
    }

    return ListTile(
      key: key,
      dense: true,
      contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 0),
      leading: isSelected && index != null
          ? Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                ReorderableDragStartListener(
                  index: index!,
                  child: const Padding(
                    padding: EdgeInsets.symmetric(horizontal: 4),
                    child: Icon(Icons.drag_handle, size: 18),
                  ),
                ),
                Text('#${index! + 1}',
                    style: TextStyle(
                      fontSize: 11,
                      fontWeight: FontWeight.bold,
                      color: theme.colorScheme.primary,
                    )),
              ],
            )
          : Icon(
              isFree ? Icons.bolt : Icons.paid,
              size: 16,
              color: isFree ? Colors.teal : Colors.orange,
            ),
      title: Row(
        children: [
          Expanded(
            child: Text(
              id,
              style: const TextStyle(
                fontFamily: 'monospace',
                fontSize: 13,
              ),
              overflow: TextOverflow.ellipsis,
            ),
          ),
          // 免费/付费标签
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
            decoration: BoxDecoration(
              color: (isFree ? Colors.teal : Colors.orange).withOpacity(0.15),
              borderRadius: BorderRadius.circular(3),
              border: Border.all(
                color: (isFree ? Colors.teal : Colors.orange).withOpacity(0.4),
                width: 0.5,
              ),
            ),
            child: Text(
              isFree ? '免费' : '付费',
              style: TextStyle(
                fontSize: 9,
                color: isFree ? Colors.teal : Colors.orange,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ),
      subtitle: Padding(
        padding: const EdgeInsets.only(top: 4),
        child: Wrap(
          spacing: 6,
          runSpacing: 4,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            if (priceText.isNotEmpty)
              Text(
                priceText,
                style: TextStyle(
                  fontSize: 10,
                  color: isFree ? Colors.teal : Colors.orange.shade700,
                  fontWeight: FontWeight.w500,
                ),
              ),
            if (ctxLen > 0)
              _MiniInfo('ctx ${_fmtCtx(ctxLen)}'),
            if (supportsTools) const _MiniInfo('工具', highlight: true),
            ...caps.take(3).map((c) => _CapabilityChip(cap: c)),
            if (desc.isNotEmpty)
              Text(
                desc,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  fontSize: 10,
                  color: theme.colorScheme.onSurfaceVariant,
                  fontStyle: FontStyle.italic,
                ),
              ),
          ],
        ),
      ),
      trailing: IconButton(
        icon: Icon(
          isSelected ? Icons.remove_circle_outline : Icons.add_circle_outline,
          size: 20,
          color: isSelected ? Colors.red : theme.colorScheme.primary,
        ),
        onPressed: onToggle,
        tooltip: isSelected ? '从该层移除' : '加入该层',
      ),
    );
  }

  static String _fmtCtx(int n) {
    if (n >= 1000000) return '${(n / 1000000).toStringAsFixed(1)}M';
    if (n >= 1000) return '${(n / 1000).toStringAsFixed(0)}K';
    return n.toString();
  }
}

/// 小型信息标签（用于 tier 模型详情）
class _MiniInfo extends StatelessWidget {
  final String text;
  final bool highlight;
  const _MiniInfo(this.text, {this.highlight = false});

  @override
  Widget build(BuildContext context) {
    final color = highlight
        ? Theme.of(context).colorScheme.primary
        : Theme.of(context).colorScheme.onSurfaceVariant;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 1),
      decoration: BoxDecoration(
        color: color.withOpacity(0.1),
        borderRadius: BorderRadius.circular(3),
      ),
      child: Text(
        text,
        style: TextStyle(fontSize: 9, color: color, fontWeight: FontWeight.w500),
      ),
    );
  }
}

/// 统一操作结果提示
void _showResult(
  BuildContext context,
  bool ok,
  ServerConfigNotifier notifier,
  String name,
) {
  if (!context.mounted) return;
  ScaffoldMessenger.of(context).showSnackBar(
    SnackBar(
      content: Text(ok ? '$name 已更新' : notifier.state.error ?? '保存失败'),
      behavior: SnackBarBehavior.floating,
    ),
  );
}
