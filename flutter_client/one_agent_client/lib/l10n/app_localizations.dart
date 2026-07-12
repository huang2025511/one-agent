import 'package:flutter/material.dart';

/// 应用国际化支持。
///
/// 通过 [AppLocalizations.of] 获取当前 context 对应的实例，
/// 通过 [delegate] 配置到 MaterialApp 的 localizationsDelegates。
class AppLocalizations {
  final Locale locale;

  AppLocalizations(this.locale);

  static AppLocalizations? of(BuildContext context) {
    return Localizations.of<AppLocalizations>(context, AppLocalizations);
  }

  static const LocalizationsDelegate<AppLocalizations> delegate =
      _AppLocalizationsDelegate();

  /// 支持的语言列表
  static const supportedLocales = [Locale('zh'), Locale('en')];

  // 翻译表
  static const _localizedValues = {
    'zh': {
      // 通用
      'appName': 'One-Agent',
      'cancel': '取消',
      'confirm': '确认',
      'delete': '删除',
      'settings': '设置',
      'copy': '复制',
      'selectAll': '全选',
      'copied': '已复制',
      'copySuccess': '已复制到剪贴板',
      // 聊天
      'chatTitle': 'One-Agent 聊天',
      'clearChat': '清空对话',
      'clearChatConfirm': '确定要清空当前对话的所有消息吗？',
      'sessionList': '会话列表',
      'inputMessage': '输入消息...',
      'send': '发送',
      'stopGenerate': '停止生成',
      'thinking': '思考中...',
      'thinkingProcess': '思考过程',
      'startChat': '开始新的对话',
      'inputBelow': '在下方输入框发送消息',
      'messageSendFailed': '消息发送失败',
      // 更新
      'checkUpdate': '检查更新',
      'newVersionAvailable': '发现新版本',
      'downloading': '下载中',
      'downloadFailed': '下载失败',
      'install': '安装',
      'alreadyLatest': '已是最新版本',
      'updateNow': '立即更新',
      'version': '版本',
    },
    'en': {
      // 通用
      'appName': 'One-Agent',
      'cancel': 'Cancel',
      'confirm': 'Confirm',
      'delete': 'Delete',
      'settings': 'Settings',
      'copy': 'Copy',
      'selectAll': 'Select All',
      'copied': 'Copied',
      'copySuccess': 'Copied to clipboard',
      // 聊天
      'chatTitle': 'One-Agent Chat',
      'clearChat': 'Clear Chat',
      'clearChatConfirm': 'Are you sure you want to clear all messages?',
      'sessionList': 'Sessions',
      'inputMessage': 'Type a message...',
      'send': 'Send',
      'stopGenerate': 'Stop',
      'thinking': 'Thinking...',
      'thinkingProcess': 'Thinking Process',
      'startChat': 'Start a new conversation',
      'inputBelow': 'Type in the input box below',
      'messageSendFailed': 'Message send failed',
      // 更新
      'checkUpdate': 'Check Update',
      'newVersionAvailable': 'New Version Available',
      'downloading': 'Downloading',
      'downloadFailed': 'Download Failed',
      'install': 'Install',
      'alreadyLatest': 'Already Latest',
      'updateNow': 'Update Now',
      'version': 'Version',
    },
  };

  // 获取翻译
  String get(String key) {
    return _localizedValues[locale.languageCode]?[key] ??
        _localizedValues['zh']![key] ??
        key;
  }

  // 方便的 getter
  // 通用
  String get appName => get('appName');
  String get cancel => get('cancel');
  String get confirm => get('confirm');
  String get delete => get('delete');
  String get settings => get('settings');
  String get copy => get('copy');
  String get selectAll => get('selectAll');
  String get copied => get('copied');
  String get copySuccess => get('copySuccess');

  // 聊天
  String get chatTitle => get('chatTitle');
  String get clearChat => get('clearChat');
  String get clearChatConfirm => get('clearChatConfirm');
  String get sessionList => get('sessionList');
  String get inputMessage => get('inputMessage');
  String get send => get('send');
  String get stopGenerate => get('stopGenerate');
  String get thinking => get('thinking');
  String get thinkingProcess => get('thinkingProcess');
  String get startChat => get('startChat');
  String get inputBelow => get('inputBelow');
  String get messageSendFailed => get('messageSendFailed');

  // 更新
  String get checkUpdate => get('checkUpdate');
  String get newVersionAvailable => get('newVersionAvailable');
  String get downloading => get('downloading');
  String get downloadFailed => get('downloadFailed');
  String get install => get('install');
  String get alreadyLatest => get('alreadyLatest');
  String get updateNow => get('updateNow');
  String get version => get('version');
}

class _AppLocalizationsDelegate
    extends LocalizationsDelegate<AppLocalizations> {
  const _AppLocalizationsDelegate();

  @override
  bool isSupported(Locale locale) => ['zh', 'en'].contains(locale.languageCode);

  @override
  Future<AppLocalizations> load(Locale locale) async {
    return AppLocalizations(locale);
  }

  @override
  bool shouldReload(_AppLocalizationsDelegate old) => false;
}
