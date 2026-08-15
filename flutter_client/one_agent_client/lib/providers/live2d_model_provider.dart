import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:archive/archive.dart';
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// 一个已导入的 Live2D 模型
@immutable
class Live2DModel {
  final String name; // 显示名
  final String dirPath; // 文件系统绝对路径（以 / 结尾）
  final String modelFileName; // xxx.model3.json

  const Live2DModel({
    required this.name,
    required this.dirPath,
    required this.modelFileName,
  });

  Map<String, dynamic> toJson() => {
        'name': name,
        'dirPath': dirPath,
        'modelFileName': modelFileName,
      };

  factory Live2DModel.fromJson(Map<String, dynamic> json) => Live2DModel(
        name: json['name'] as String,
        dirPath: json['dirPath'] as String,
        modelFileName: json['modelFileName'] as String,
      );
}

/// Live2D 模型管理状态
class Live2DModelState {
  final List<Live2DModel> models; // 已导入的模型
  final int currentIndex; // 当前选中的模型，-1 表示用 assets/Canvas
  final bool isImporting;
  final String? error;

  const Live2DModelState({
    this.models = const [],
    this.currentIndex = -1,
    this.isImporting = false,
    this.error,
  });

  /// 当前选中的模型（null 表示用 assets 或 Canvas fallback）
  Live2DModel? get currentModel =>
      currentIndex >= 0 && currentIndex < models.length
          ? models[currentIndex]
          : null;

  Live2DModelState copyWith({
    List<Live2DModel>? models,
    int? currentIndex,
    bool? isImporting,
    String? error,
  }) =>
      Live2DModelState(
        models: models ?? this.models,
        currentIndex: currentIndex ?? this.currentIndex,
        isImporting: isImporting ?? this.isImporting,
        error: error,
      );
}

/// Live2D 模型管理 Provider
class Live2DModelNotifier extends StateNotifier<Live2DModelState> {
  Live2DModelNotifier() : super(const Live2DModelState()) {
    _load();
  }

  static const _kModels = 'live2d_models';
  static const _kCurrentIndex = 'live2d_current_index';

  Future<void> _load() async {
    final prefs = await SharedPreferences.getInstance();
    final list = prefs.getStringList(_kModels) ?? [];
    final models =
        list.map((s) => Live2DModel.fromJson(_decode(s))).toList();
    final idx = prefs.getInt(_kCurrentIndex) ?? -1;
    state = state.copyWith(
      models: models,
      currentIndex: idx.clamp(-1, models.length - 1),
    );
  }

  Future<void> _persist() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setStringList(
      _kModels,
      state.models.map((m) => _encode(m.toJson())).toList(),
    );
    await prefs.setInt(_kCurrentIndex, state.currentIndex);
  }

  String _encode(Map<String, dynamic> m) {
    // 简单序列化：key=value|key=value
    return m.entries.map((e) => '${e.key}=${e.value}').join('|');
  }

  Map<String, dynamic> _decode(String s) {
    final map = <String, dynamic>{};
    for (final part in s.split('|')) {
      final idx = part.indexOf('=');
      if (idx > 0) {
        map[part.substring(0, idx)] = part.substring(idx + 1);
      }
    }
    return map;
  }

  /// 导入 zip 模型包
  ///
  /// 解压到 app 文档目录下的 live2d_models/<模型名>/
  /// 自动查找 .model3.json
  Future<bool> importZip(String zipPath) async {
    state = state.copyWith(isImporting: true, error: null);
    try {
      final bytes = await File(zipPath).readAsBytes();
      final archive = ZipDecoder().decodeBytes(bytes);

      // 用 zip 文件名作为模型目录名（去扩展名）
      final zipName = zipPath.split('/').last;
      final modelName = zipName.replaceAll(RegExp(r'\.zip$', caseSensitive: false), '');

      final docsDir = await getApplicationDocumentsDirectory();
      final modelRoot = '${docsDir.path}/live2d_models/$modelName';
      // 清理旧目录
      final rootDir = Directory(modelRoot);
      if (rootDir.existsSync()) rootDir.deleteSync(recursive: true);

      // 解压（含 Zip Slip 防护：拒绝绝对路径与 ../ 路径穿越条目）
      final rootPath = Directory(modelRoot).absolute.path;
      String? model3JsonPath;
      for (final file in archive) {
        // 规范化条目名：反斜杠转正斜杠（Windows 打包的 zip）
        var entryName = file.name.replaceAll('\\', '/');
        // 拒绝绝对路径与 .. 穿越
        if (entryName.startsWith('/') || entryName.split('/').contains('..')) {
          throw Exception('zip 包含非法路径条目: ${file.name}');
        }
        if (entryName.isEmpty) continue;
        final outFile = File('$modelRoot/$entryName');
        // 双重校验：解析后的绝对路径必须仍在模型目录内
        if (!outFile.absolute.path.startsWith('$rootPath/')) {
          throw Exception('zip 路径越界: ${file.name}');
        }
        if (file.isFile) {
          await outFile.parent.create(recursive: true);
          await outFile.writeAsBytes(file.content as List<int>);
          if (entryName.toLowerCase().endsWith('.model3.json')) {
            model3JsonPath = entryName;
          }
        } else {
          await outFile.create(recursive: true);
        }
      }

      if (model3JsonPath == null) {
        // 在解压后的目录里再找一遍（有些 zip 有嵌套目录）
        final found = await _findModel3Json(rootDir);
        if (found == null) {
          state = state.copyWith(
            isImporting: false,
            error: '未在 zip 中找到 .model3.json 文件（可能不是 Live2D Cubism 3.0+ 模型）',
          );
          return false;
        }
        model3JsonPath = found;
      }

      // 计算模型文件所在目录的相对路径
      final modelFile = File('$modelRoot/$model3JsonPath');
      final modelDir = modelFile.parent.path;
      final fileName = model3JsonPath.split('/').last;

      final model = Live2DModel(
        name: modelName,
        dirPath: modelDir.endsWith('/') ? modelDir : '$modelDir/',
        modelFileName: fileName,
      );

      // 重复导入同名模型时替换旧条目（旧目录已在上方删除重建），
      // 避免列表里出现两个同名模型
      final newModels = [
        ...state.models.where((m) => m.name != modelName),
        model,
      ];
      state = state.copyWith(
        models: newModels,
        currentIndex: newModels.length - 1, // 自动选中新导入的
        isImporting: false,
      );
      await _persist();
      return true;
    } catch (e) {
      state = state.copyWith(isImporting: false, error: '导入失败: $e');
      return false;
    }
  }

  /// 递归查找 .model3.json
  Future<String?> _findModel3Json(Directory dir) async {
    try {
      await for (final entity in dir.list(recursive: true)) {
        if (entity is File &&
            entity.path.toLowerCase().endsWith('.model3.json')) {
          return entity.path.substring(dir.path.length + 1);
        }
      }
    } catch (_) {}
    return null;
  }

  /// 选中某个模型（index = -1 表示用 assets/Canvas）
  Future<void> setCurrent(int index) async {
    state = state.copyWith(currentIndex: index);
    await _persist();
  }

  /// 删除某个模型
  Future<void> removeAt(int index) async {
    if (index < 0 || index >= state.models.length) return;
    final model = state.models[index];
    // 删除文件
    try {
      Directory(model.dirPath).deleteSync(recursive: true);
    } catch (_) {}
    final newModels = [...state.models]..removeAt(index);
    var newIdx = state.currentIndex;
    if (index == newIdx) {
      newIdx = -1;
    } else if (index < newIdx) {
      newIdx -= 1;
    }
    state = state.copyWith(models: newModels, currentIndex: newIdx);
    await _persist();
  }
}

final live2dModelProvider =
    StateNotifierProvider<Live2DModelNotifier, Live2DModelState>(
  (ref) => Live2DModelNotifier(),
);
