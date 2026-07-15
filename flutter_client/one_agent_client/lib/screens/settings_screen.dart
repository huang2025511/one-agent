import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

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
    final routingOn = notifier.catalogRoutingEnabled;
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
        // 默认模型展示（Hero 区域）
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
                ],
              ),
              const SizedBox(height: 12),
              Text(
                defaultModel,
                style: theme.textTheme.titleMedium?.copyWith(
                  fontFamily: 'monospace',
                  fontWeight: FontWeight.bold,
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

        // 路由子选项（仅路由开启时显示）
        if (routingOn) ...[
          _SwitchTile(
            title: '自我进化',
            subtitle: '根据历史成功率自动调整 tier 阈值',
            value: notifier.selfEvolutionEnabled,
            onChanged: (v) => notifier.updateConfig({
              'router': {'self_evolution': {'enabled': v}}
            }).then((ok) => _showResult(context, ok, notifier, '自我进化')),
          ),
          _SwitchTile(
            title: '上下文压缩',
            subtitle: '超长对话自动压缩历史上下文',
            value: notifier.contextCompressionEnabled,
            onChanged: (v) => notifier.updateConfig({
              'router': {'context_compression': {'enabled': v}}
            }).then((ok) => _showResult(context, ok, notifier, '上下文压缩')),
          ),
          _SwitchTile(
            title: '技能懒加载',
            subtitle: '按需加载技能，降低 token 消耗',
            value: notifier.skillLazyLoadingEnabled,
            onChanged: (v) => notifier.updateConfig({
              'router': {'skill_lazy_loading': {'enabled': v}}
            }).then((ok) => _showResult(context, ok, notifier, '技能懒加载')),
          ),
        ],

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
            final models = tier['models'] as List? ?? [];
            final picked = (tier['stats'] as Map?)?['picked'] ?? 0;
            return _TierRow(
              name: e.key,
              modelCount: models.length,
              threshold: (tier['threshold'] as num?)?.toDouble() ?? 0,
              tokenBudget: tier['token_budget'] ?? 0,
              picked: picked,
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
                final count = (e.value as List).length;
                return _CategoryChip(category: e.key, count: count);
              }).toList(),
            ),
          ),
        ] else ...[
          const SizedBox(height: 8),
        ],

        // 编辑默认模型
        _NavTile(
          title: '默认模型',
          subtitle: '设置 llm.primary_model',
          value: defaultModel,
          leading: Icons.edit_outlined,
          onTap: () => _showTextEditDialog(
            context,
            title: '默认模型',
            label: 'provider/model',
            initial: defaultModel,
            onSubmit: (v) => notifier.updateConfig({
              'llm': {'primary_model': v}
            }),
          ),
        ),
        // 轻量模型
        _NavTile(
          title: '轻量模型',
          subtitle: '路由摘要、意图分类用',
          value: notifier.lightweightModel,
          leading: Icons.flash_on,
          onTap: () => _showTextEditDialog(
            context,
            title: '轻量模型',
            label: 'provider/model',
            initial: notifier.lightweightModel,
            onSubmit: (v) => notifier.updateConfig({
              'llm': {'lightweight_model': v}
            }),
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

/// Tier 行
class _TierRow extends StatelessWidget {
  final String name;
  final int modelCount;
  final double threshold;
  final int tokenBudget;
  final int picked;

  const _TierRow({
    required this.name,
    required this.modelCount,
    required this.threshold,
    required this.tokenBudget,
    required this.picked,
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
      child: Row(
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
              '$modelCount 模型 · 阈值≥$threshold · ${tokenBudget}T',
              style: theme.textTheme.bodySmall?.copyWith(
                fontFamily: 'monospace',
                color: theme.colorScheme.onSurfaceVariant,
              ),
            ),
          ),
          _InfoChip('$picked 次', color: color),
        ],
      ),
    );
  }
}

/// 分类标签
class _CategoryChip extends StatelessWidget {
  final String category;
  final int count;

  const _CategoryChip({required this.category, required this.count});

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

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final icon = _icons[category] ?? Icons.category;
    final label = _labels[category] ?? category;

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: theme.colorScheme.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(10),
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
    return _SettingsSection(
      icon: Icons.shield,
      title: '安全',
      children: [
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
//  关于分区
// ════════════════════════════════════════════════════════════════
class _AboutSection extends ConsumerWidget {
  const _AboutSection();

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final updateState = ref.watch(updateProvider);

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
          subtitle: updateState.error != null
              ? Text(updateState.error!,
                  style: const TextStyle(fontSize: 12, color: Colors.red))
              : updateState.latestRelease != null
                  ? Text('新版本可用: v${updateState.latestRelease!.version}',
                      style: const TextStyle(fontSize: 12, color: Colors.orange))
                  : null,
          onTap: updateState.isChecking || updateState.isDownloading
              ? null
              : () => ref.read(updateProvider.notifier).checkForUpdate(),
          contentPadding: const EdgeInsets.symmetric(horizontal: 20),
        ),
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
