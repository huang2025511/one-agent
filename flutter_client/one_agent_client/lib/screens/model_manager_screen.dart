import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:file_picker/file_picker.dart';
import 'package:url_launcher/url_launcher.dart';

import '../pet/overlay_pet_service.dart';
import '../providers/live2d_model_provider.dart';

/// Live2D 官方免费模型列表（可商用，个人/小企业）
class _OfficialModel {
  final String name;
  final String desc;
  final String url;
  const _OfficialModel(this.name, this.desc, this.url);
}

const _officialModels = [
  _OfficialModel('Haru 春', '动作+表情+语音齐全，推荐首选',
      'https://www.live2d.com/en/learn/sample/haru/'),
  _OfficialModel('Hiyori 桃濑日和', 'Cubism 3.0 标准模型，简洁易用',
      'https://www.live2d.com/en/learn/sample/momose-hiyori/'),
  _OfficialModel('Mark 马克君', '结构简单，适合入门',
      'https://www.live2d.com/en/learn/sample/mark/'),
  _OfficialModel('Rice Glassfield', '扩展插值+反转蒙版，表现力强',
      'https://www.live2d.com/en/learn/sample/rice-glassfield/'),
  _OfficialModel('Epsilon 伊普西隆', '标准易用模型',
      'https://www.live2d.com/en/learn/sample/epsilon/'),
  _OfficialModel('Tororo & Hijiki 猫', '白猫+黑猫，可爱',
      'https://www.live2d.com/en/learn/sample/tororo-hijiki/'),
  _OfficialModel('Natori 名执尽', '男性模型，手腕切换',
      'https://www.live2d.com/en/learn/sample/natori-jin/'),
  _OfficialModel('Kei 京', '真实唇形同步 motion-sync',
      'https://www.live2d.com/en/learn/sample/kei/'),
];

/// Live2D 模型管理页面
class ModelManagerScreen extends ConsumerWidget {
  const ModelManagerScreen({super.key});

  /// 悬浮窗运行中时，把当前选中的模型推送给原生服务实时切换。
  /// [model] 为 null 表示切回内置 Hiyori 模型。
  Future<void> _pushModelToOverlay(Live2DModel? model) async {
    final service = OverlayPetService();
    await service.syncState();
    if (service.isActive) {
      await service.updateModel(
        modelPath: model?.dirPath,
        modelFileName: model?.modelFileName,
      );
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final state = ref.watch(live2dModelProvider);
    final notifier = ref.read(live2dModelProvider.notifier);
    final theme = Theme.of(context);

    return Scaffold(
      appBar: AppBar(title: const Text('Live2D 模型管理')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          // ── 当前模型状态 ──
          Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Row(
                children: [
                  Icon(Icons.pets, color: theme.colorScheme.primary),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text('当前模型',
                            style: theme.textTheme.labelMedium),
                        Text(
                          state.currentModel?.name ?? '内置 Hiyori（未导入模型）',
                          style: theme.textTheme.titleMedium,
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ),
          const SizedBox(height: 16),

          // ── 导入模型 ──
          FilledButton.icon(
            onPressed: state.isImporting
                ? null
                : () => _importZip(context, notifier),
            icon: state.isImporting
                ? const SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Icon(Icons.file_upload_outlined),
            label: Text(state.isImporting ? '导入中...' : '导入模型 ZIP 文件'),
          ),
          if (state.error != null) ...[
            const SizedBox(height: 8),
            Text(state.error!,
                style: TextStyle(color: theme.colorScheme.error, fontSize: 13)),
          ],
          const SizedBox(height: 8),
          Text(
            '下载官方模型 ZIP 后，点上方按钮选择文件，自动解压导入。导入后会自动切换为新模型。',
            style: theme.textTheme.bodySmall?.copyWith(color: theme.hintColor),
          ),
          const SizedBox(height: 24),

          // ── 已导入模型列表 ──
          Text('已导入的模型', style: theme.textTheme.titleSmall),
          const SizedBox(height: 8),
          if (state.models.isEmpty)
            Card(
              child: ListTile(
                leading: Icon(Icons.inbox_outlined, color: theme.hintColor),
                title: const Text('还没有导入模型'),
                subtitle: const Text('从下方官方模型库下载 ZIP 后导入'),
              ),
            )
          else
            ...List.generate(state.models.length, (i) {
              final m = state.models[i];
              final selected = i == state.currentIndex;
              return Card(
                child: ListTile(
                  leading: Icon(
                    selected ? Icons.check_circle : Icons.pets_outlined,
                    color: selected ? theme.colorScheme.primary : null,
                  ),
                  title: Text(m.name),
                  subtitle: Text(
                    m.modelFileName,
                    style: const TextStyle(fontSize: 11),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                  ),
                  trailing: PopupMenuButton<String>(
                    onSelected: (v) async {
                      if (v == 'select') {
                        await notifier.setCurrent(i);
                        await _pushModelToOverlay(m);
                      }
                      if (v == 'delete') {
                        _confirmDelete(context, notifier, m.name, i);
                      }
                    },
                    itemBuilder: (_) => [
                      const PopupMenuItem(
                          value: 'select', child: Text('设为当前模型')),
                      const PopupMenuItem(
                          value: 'delete',
                          child: Text('删除',
                              style: TextStyle(color: Colors.red))),
                    ],
                  ),
                  onTap: () async {
                    await notifier.setCurrent(i);
                    await _pushModelToOverlay(m);
                  },
                ),
              );
            }),

          // 单独选项：使用内置/Canvas
          if (state.models.isNotEmpty) ...[
            const SizedBox(height: 8),
          Card(
            child: ListTile(
              leading: Icon(
                state.currentIndex == -1
                    ? Icons.check_circle
                    : Icons.bubble_chart_outlined,
                color: state.currentIndex == -1
                    ? theme.colorScheme.primary
                    : null,
              ),
              title: const Text('内置 Hiyori 模型'),
              subtitle: const Text('不使用导入的模型，使用内置官方示例模型'),
              onTap: () async {
                await notifier.setCurrent(-1);
                await _pushModelToOverlay(null);
              },
            ),
          ),
          ],
          const SizedBox(height: 24),

          // ── 官方免费模型库 ──
          Text('官方免费模型库（可商用）', style: theme.textTheme.titleSmall),
          const SizedBox(height: 4),
          Text(
            '点击「打开下载页」跳转浏览器，在官网点 Download 下载 ZIP（选 For SmartPhone 版本体积更小），然后回到本页点「导入模型 ZIP」。',
            style: theme.textTheme.bodySmall?.copyWith(color: theme.hintColor),
          ),
          const SizedBox(height: 8),
          ..._officialModels.map((m) => Card(
                child: ListTile(
                  leading: const Icon(Icons.download_for_offline_outlined),
                  title: Text(m.name),
                  subtitle: Text(m.desc,
                      style: const TextStyle(fontSize: 12)),
                  trailing: OutlinedButton(
                    onPressed: () => _launchUrl(m.url),
                    child: const Text('打开下载页'),
                  ),
                ),
              )),
          const SizedBox(height: 16),
          // 协议说明
          Card(
            color: theme.colorScheme.surfaceContainerHighest,
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(Icons.info_outline,
                      size: 18, color: theme.colorScheme.primary),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      'Live2D 官方示例模型允许个人和小企业（年销售额 < 1000 万日元）免费商用。详见官网许可协议。',
                      style: theme.textTheme.bodySmall,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _importZip(
      BuildContext context, Live2DModelNotifier notifier) async {
    final result = await FilePicker.platform.pickFiles(
      type: FileType.custom,
      allowedExtensions: ['zip'],
    );
    if (result == null || result.files.single.path == null) return;
    final ok = await notifier.importZip(result.files.single.path!);
    if (ok) {
      // 导入成功后自动切换为新模型，并推送给运行中的悬浮窗
      await _pushModelToOverlay(notifier.state.currentModel);
    }
    if (!context.mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(ok ? '导入成功，已切换为新模型' : '导入失败，请检查 ZIP 是否为 Live2D Cubism 3.0+ 模型'),
      ),
    );
  }

  Future<void> _confirmDelete(BuildContext context,
      Live2DModelNotifier notifier, String name, int index) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('删除模型'),
        content: Text('确定删除「$name」？文件也会被清除。'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(ctx, false),
              child: const Text('取消')),
          TextButton(
              onPressed: () => Navigator.pop(ctx, true),
              child: const Text('删除',
                  style: TextStyle(color: Colors.red))),
        ],
      ),
    );
    if (ok == true) await notifier.removeAt(index);
  }

  Future<void> _launchUrl(String url) async {
    final uri = Uri.parse(url);
    if (await canLaunchUrl(uri)) {
      await launchUrl(uri, mode: LaunchMode.externalApplication);
    }
  }
}
