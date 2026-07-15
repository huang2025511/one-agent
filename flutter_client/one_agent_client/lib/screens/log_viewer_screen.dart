import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../api/system_api.dart';

/// 日志查看页面 — 浏览服务端运行日志
///
/// 支持关键词搜索、级别过滤（DEBUG/INFO/WARNING/ERROR）、行数控制，
/// 按级别着色显示，时间戳使用 monospace 字体。
/// 支持切换「本次启动 / 全部日志」——
///   本次启动：仅展示 one-agent 本次启动以来的日志（默认）；
///   全部日志：展示日志文件中所有历史日志。
class LogViewerScreen extends ConsumerStatefulWidget {
  const LogViewerScreen({super.key});

  @override
  ConsumerState<LogViewerScreen> createState() => _LogViewerScreenState();
}

class _LogViewerScreenState extends ConsumerState<LogViewerScreen> {
  final _searchController = TextEditingController();

  /// null 表示"全部"级别
  String? _level;
  int _tail = 500;
  String _search = '';

  /// true = 本次启动以来的日志（服务端默认 since=_ctx.started_at）
  /// false = 全部历史日志（since=0）
  bool _sinceBoot = true;

  List<String> _lines = [];
  int _total = 0;
  int _filtered = 0;
  bool _isLoading = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    // 监听搜索框文本变化以动态显示清除按钮
    _searchController.addListener(() {
      if (mounted) setState(() {});
    });
    WidgetsBinding.instance.addPostFrameCallback((_) => _load());
  }

  @override
  void dispose() {
    _searchController.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    setState(() {
      _isLoading = true;
      _error = null;
    });
    try {
      final result = await SystemApi.getLogs(
        tail: _tail,
        level: _level,
        search: _search.isEmpty ? null : _search,
        // sinceBoot=true → null（服务端使用 _ctx.started_at）
        // sinceBoot=false → 0（查看全部历史日志）
        since: _sinceBoot ? null : 0,
      );
      if (!mounted) return;
      if (result == null) {
        setState(() {
          _isLoading = false;
          _error = '加载日志失败，请检查服务器连接';
        });
        return;
      }
      final lines = (result['lines'] as List<dynamic>? ?? [])
          .map((e) => e.toString())
          .toList();
      setState(() {
        _lines = lines;
        _total = (result['total'] as num?)?.toInt() ?? lines.length;
        _filtered =
            (result['filtered'] as num?)?.toInt() ?? lines.length;
        _isLoading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _isLoading = false;
        _error = e.toString();
      });
    }
  }

  void _onSearch() {
    _search = _searchController.text.trim();
    _load();
  }

  /// 解析单行日志：`2026-07-15 10:23:45 | WARNING | api | 消息内容`
  _ParsedLog _parseLine(String line) {
    final pattern = RegExp(
      r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*(\w+)\s*\|\s*([^|]+?)\s*\|\s*(.*)$',
    );
    final m = pattern.firstMatch(line);
    if (m == null) {
      return _ParsedLog(timestamp: null, level: null, module: null, message: line);
    }
    return _ParsedLog(
      timestamp: m.group(1),
      level: m.group(2)?.toUpperCase(),
      module: m.group(3),
      message: m.group(4) ?? '',
    );
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(
        title: const Text('日志查看'),
        actions: [
          // 切换「本次启动 / 全部日志」
          Tooltip(
            message: _sinceBoot ? '当前：本次启动' : '当前：全部日志',
            child: TextButton.icon(
              onPressed: _isLoading
                  ? null
                  : () {
                      setState(() => _sinceBoot = !_sinceBoot);
                      _load();
                    },
              icon: Icon(
                _sinceBoot ? Icons.flash_on : Icons.history,
                size: 18,
              ),
              label: Text(
                _sinceBoot ? '本次启动' : '全部日志',
                style: const TextStyle(fontSize: 13),
              ),
              style: TextButton.styleFrom(
                padding: const EdgeInsets.symmetric(horizontal: 8),
                visualDensity: VisualDensity.compact,
              ),
            ),
          ),
          IconButton(
            icon: const Icon(Icons.refresh),
            tooltip: '刷新',
            onPressed: _isLoading ? null : _load,
          ),
        ],
      ),
      body: Column(
        children: [
          _buildToolbar(context),
          if (_error != null)
            Container(
              width: double.infinity,
              color: theme.colorScheme.errorContainer,
              padding: const EdgeInsets.all(12),
              child: Row(
                children: [
                  Icon(Icons.error_outline,
                      color: theme.colorScheme.onErrorContainer, size: 18),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      _error!,
                      style:
                          TextStyle(color: theme.colorScheme.onErrorContainer),
                    ),
                  ),
                  TextButton(
                    onPressed: _load,
                    child: const Text('重试'),
                  ),
                ],
              ),
            ),
          Expanded(child: _buildList(context, theme)),
          _buildStatusBar(context, theme),
        ],
      ),
    );
  }

  Widget _buildToolbar(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 12, 12, 4),
      child: Column(
        children: [
          // 搜索框
          TextField(
            controller: _searchController,
            textInputAction: TextInputAction.search,
            decoration: InputDecoration(
              hintText: '搜索关键词...',
              isDense: true,
              prefixIcon: const Icon(Icons.search, size: 20),
              suffixIcon: _searchController.text.isNotEmpty
                  ? IconButton(
                      icon: const Icon(Icons.clear, size: 20),
                      onPressed: () {
                        _searchController.clear();
                        if (_search.isNotEmpty) {
                          _search = '';
                          _load();
                        }
                      },
                    )
                  : null,
            ),
            onSubmitted: (_) => _onSearch(),
            onTapOutside: (_) => FocusScope.of(context).unfocus(),
          ),
          const SizedBox(height: 8),
          // 级别 + 行数
          Row(
            children: [
              Expanded(child: _buildLevelDropdown()),
              const SizedBox(width: 8),
              Expanded(child: _buildTailDropdown()),
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildLevelDropdown() {
    const levels = <String?>[null, 'DEBUG', 'INFO', 'WARNING', 'ERROR'];
    const labels = ['全部', 'DEBUG', 'INFO', 'WARNING', 'ERROR'];
    return DropdownButtonFormField<String?>(
      value: _level,
      isDense: true,
      decoration: const InputDecoration(
        labelText: '级别',
        isDense: true,
        prefixIcon: Icon(Icons.filter_list, size: 20),
      ),
      items: List.generate(levels.length, (i) {
        return DropdownMenuItem<String?>(
          value: levels[i],
          child: Text(labels[i]),
        );
      }),
      onChanged: _isLoading
          ? null
          : (v) {
              setState(() => _level = v);
              _load();
            },
    );
  }

  Widget _buildTailDropdown() {
    const tails = [100, 500, 1000, 2000];
    return DropdownButtonFormField<int>(
      value: _tail,
      isDense: true,
      decoration: const InputDecoration(
        labelText: '行数',
        isDense: true,
        prefixIcon: Icon(Icons.format_list_numbered, size: 20),
      ),
      items: tails
          .map((t) => DropdownMenuItem(value: t, child: Text('$t 行')))
          .toList(),
      onChanged: _isLoading
          ? null
          : (v) {
              if (v == null) return;
              setState(() => _tail = v);
              _load();
            },
    );
  }

  Widget _buildList(BuildContext context, ThemeData theme) {
    if (_isLoading && _lines.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_lines.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.article_outlined,
                size: 64, color: theme.colorScheme.outlineVariant),
            const SizedBox(height: 16),
            Text(
              '暂无日志',
              style: theme.textTheme.titleMedium?.copyWith(
                color: theme.colorScheme.outline,
              ),
            ),
          ],
        ),
      );
    }
    // 用 SelectionArea 包裹 ListView，方便复制多行内容
    return SelectionArea(
      child: ListView.builder(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        itemCount: _lines.length,
        itemBuilder: (context, index) {
          final parsed = _parseLine(_lines[index]);
          return _LogLine(parsed: parsed);
        },
      ),
    );
  }

  Widget _buildStatusBar(BuildContext context, ThemeData theme) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      decoration: BoxDecoration(
        color: theme.colorScheme.surfaceContainerHighest,
        border: Border(
          top: BorderSide(color: theme.colorScheme.outlineVariant),
        ),
      ),
      child: Row(
        children: [
          Icon(
            _sinceBoot ? Icons.flash_on : Icons.history,
            size: 14,
            color: _sinceBoot
                ? theme.colorScheme.primary
                : theme.colorScheme.outline,
          ),
          const SizedBox(width: 4),
          Text(
            _sinceBoot ? '本次启动' : '全部日志',
            style: theme.textTheme.labelSmall?.copyWith(
              color: _sinceBoot
                  ? theme.colorScheme.primary
                  : theme.colorScheme.outline,
              fontWeight: _sinceBoot ? FontWeight.w600 : null,
            ),
          ),
          const SizedBox(width: 12),
          Icon(Icons.list_alt, size: 14, color: theme.colorScheme.outline),
          const SizedBox(width: 4),
          Text('总 $_total 行', style: theme.textTheme.labelSmall),
          const SizedBox(width: 16),
          Icon(Icons.filter_alt, size: 14, color: theme.colorScheme.outline),
          const SizedBox(width: 4),
          Text('过滤 $_filtered 行', style: theme.textTheme.labelSmall),
          const Spacer(),
          if (_isLoading)
            SizedBox(
              width: 12,
              height: 12,
              child: CircularProgressIndicator(
                strokeWidth: 2,
                color: theme.colorScheme.outline,
              ),
            )
          else
            Text(
              '显示 ${_lines.length} 行',
              style: theme.textTheme.labelSmall?.copyWith(
                color: theme.colorScheme.outline,
              ),
            ),
        ],
      ),
    );
  }
}

/// 解析后的单行日志
class _ParsedLog {
  final String? timestamp;
  final String? level;
  final String? module;
  final String message;

  _ParsedLog({
    this.timestamp,
    this.level,
    this.module,
    required this.message,
  });
}

/// 单行日志展示 — 时间戳用 monospace，按级别着色
class _LogLine extends StatelessWidget {
  final _ParsedLog parsed;

  const _LogLine({required this.parsed});

  /// 按级别返回颜色：DEBUG=灰色、INFO=默认、WARNING=橙色、ERROR=红色
  Color? _levelColor(ThemeData theme) {
    switch (parsed.level) {
      case 'DEBUG':
        return theme.colorScheme.outline;
      case 'INFO':
        return null;
      case 'WARNING':
      case 'WARN':
        return Colors.orange;
      case 'ERROR':
      case 'CRITICAL':
      case 'FATAL':
        return theme.colorScheme.error;
      default:
        return null;
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final color = _levelColor(theme);

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 2, horizontal: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (parsed.timestamp != null) ...[
            SelectableText(
              parsed.timestamp!,
              style: theme.textTheme.bodySmall?.copyWith(
                fontFamily: 'monospace',
                color: theme.colorScheme.outline,
                fontSize: 12,
                height: 1.3,
              ),
            ),
            const SizedBox(width: 8),
          ],
          if (parsed.level != null) ...[
            Container(
              padding:
                  const EdgeInsets.symmetric(horizontal: 4, vertical: 1),
              decoration: BoxDecoration(
                color: (color ?? theme.colorScheme.primary)
                    .withOpacity(0.15),
                borderRadius: BorderRadius.circular(4),
              ),
              child: Text(
                parsed.level!,
                style: theme.textTheme.labelSmall?.copyWith(
                  color: color ?? theme.colorScheme.primary,
                  fontWeight: FontWeight.w600,
                  fontSize: 10,
                  fontFamily: 'monospace',
                ),
              ),
            ),
            const SizedBox(width: 8),
          ],
          if (parsed.module != null && parsed.module!.isNotEmpty) ...[
            SelectableText(
              '[${parsed.module}]',
              style: theme.textTheme.bodySmall?.copyWith(
                color: theme.colorScheme.outline,
                fontSize: 12,
                height: 1.3,
              ),
            ),
            const SizedBox(width: 8),
          ],
          Expanded(
            child: SelectableText(
              parsed.message,
              style: theme.textTheme.bodySmall?.copyWith(
                color: color,
                fontFamily: 'monospace',
                fontSize: 12,
                height: 1.3,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
