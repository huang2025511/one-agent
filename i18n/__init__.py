"""Internationalization (i18n) module — multi-language support.

Provides:
  - Language detection from config and user input
  - Translation function _() for user-facing messages
  - Built-in translations for Chinese, English, Japanese, Korean
  - Runtime language switching
  - Auto-detection of user language from input text
  - Translation memory
  - Community translation contributions
  - Language pack management
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Lock for thread-safe access to global state
_lock = threading.RLock()

# Thread-local storage for per-thread language preference
_thread_local = threading.local()

# Current language (default: English) — global default
_current_lang = "en"

# Track if language was auto-detected (to avoid repeated switching)
_auto_detected = False

# Translation dictionaries
_translations: Dict[str, Dict[str, str]] = {
    "en": {
        # API errors
        "rate_limit_exceeded": "rate limit exceeded",
        "request_body_too_large": "request body too large ({size} > {max})",
        "invalid_api_key": "Invalid API key",
        "not_ready": "not ready",
        "memory_not_available": "memory not available",
        "skills_not_available": "skills not available",
        "llm_not_available": "llm not available",
        "unknown_key": "unknown key: {key}",
        "sensitive_setting_protected": "{alias} is sensitive — enable security.allow_sensitive_chat_settings to modify via API",
        "cannot_parse_value": "cannot parse value for type {type}",
        "backup_create_failed": "failed to create backup",
        "restore_failed": "failed to restore config",
        "backup_not_found": "backup not found: {filename}",
        "missing_field": "missing field: {field}",
        "internal_error": "internal server error",
        "agent_not_ready": "agent not ready",
        "alert_manager_not_available": "alert manager not available",
        "cost_tracking_not_available": "cost tracking not available",
        "need_key_and_value": "need key and value",
        "approval_manager_not_available": "approval manager not available",
        "approval_request_not_found": "approval request not found: {request_id}",
        "session_not_found": "session not found: {session_id}",
        "document_not_found": "document not found: {name}",
        "need_file_or_path": "need file or path",
        "session_store_not_available": "session store not available",
        "improvement_not_available": "self-improvement not available",

        # Model errors
        "no_api_key": "[no API key configured for provider '{provider}']",
        "service_unavailable": "[service temporarily unavailable — please retry later]",

        # CLI messages
        "welcome": "╔══════════════════════════════════════════════╗\n║  One-Agent v2 — Natural language interface, type 'help'   ║\n╚══════════════════════════════════════════════╝",
        "timeout": "[timeout — try again]",
        "shutting_down": "[shutting down...]",
        "cli_help_content": "You can use natural language or precise commands:\n  exit/quit/bye       → Exit program\n  help/?              → Show help\n  status              → System status\n  clear               → Clear screen\n  Any other text      → Chat with AI",

        # Common
        "ok": "ok",
        "error": "error",
        "success": "success",
        "failed": "failed",
    },
    "zh": {
        # API errors
        "rate_limit_exceeded": "请求频率超限",
        "request_body_too_large": "请求体过大（{size} > {max}）",
        "invalid_api_key": "无效的 API 密钥",
        "not_ready": "未就绪",
        "memory_not_available": "内存服务不可用",
        "skills_not_available": "技能服务不可用",
        "llm_not_available": "LLM 服务不可用",
        "unknown_key": "未知的配置项: {key}",
        "sensitive_setting_protected": "{alias} 是敏感配置 — 请启用 security.allow_sensitive_chat_settings 以通过 API 修改",
        "cannot_parse_value": "无法解析 {type} 类型的值",
        "backup_create_failed": "创建备份失败",
        "restore_failed": "恢复配置失败",
        "backup_not_found": "备份不存在: {filename}",
        "missing_field": "缺少字段: {field}",
        "internal_error": "内部服务器错误",
        "agent_not_ready": "代理未就绪",
        "alert_manager_not_available": "告警管理器不可用",
        "cost_tracking_not_available": "成本追踪不可用",
        "need_key_and_value": "需要提供 key 和 value",
        "approval_manager_not_available": "审批管理器不可用",
        "approval_request_not_found": "审批请求不存在: {request_id}",
        "session_not_found": "会话不存在: {session_id}",
        "document_not_found": "文档不存在: {name}",
        "need_file_or_path": "需要提供文件或路径",
        "session_store_not_available": "会话存储不可用",
        "improvement_not_available": "自我改进服务不可用",

        # Model errors
        "no_api_key": "[未配置提供商 '{provider}' 的 API 密钥]",
        "service_unavailable": "[服务暂时不可用 — 请稍后重试]",

        # CLI messages
        "welcome": "╔══════════════════════════════════════════════╗\n║  One-Agent v2 — 自然语言即可操作，输入 '帮助'   ║\n╚══════════════════════════════════════════════╝",
        "timeout": "[超时 — 请重试]",
        "shutting_down": "[正在关闭...]",
        "cli_help_content": "你可以用自然语言操作，也可以用精准命令：\n  退出/再见/bye     → 退出程序\n  帮助/怎么用/help  → 显示帮助\n  状态/运行情况     → 系统状态\n  清屏/clear        → 清除屏幕\n  其他任何文字      → 与 AI 对话",

        # Common
        "ok": "正常",
        "error": "错误",
        "success": "成功",
        "failed": "失败",
    },
}


def set_language(lang: str) -> None:
    """Set the current language.

    Args:
        lang: Language code ('en' or 'zh')
    """
    global _current_lang, _auto_detected
    with _lock:
        if lang in _translations:
            _current_lang = lang
            _auto_detected = False  # Manual set, not auto-detected
            logger.info("language set to: %s", lang)
        else:
            logger.warning("unsupported language: %s, falling back to English", lang)
            _current_lang = "en"


def get_language() -> str:
    """Get the current language code."""
    # Check thread-local first, then fall back to global
    thread_lang = getattr(_thread_local, 'lang', None)
    if thread_lang:
        return thread_lang
    with _lock:
        return _current_lang


def set_thread_language(lang: str) -> None:
    """Set language for the current thread only.

    Used by REST API handlers to isolate per-request language without
    affecting other concurrent requests (the global _current_lang is
    shared across all threads and would cause multi-tenant language
    contention).
    """
    if lang in _translations:
        _thread_local.lang = lang
    else:
        _thread_local.lang = "en"


def clear_thread_language() -> None:
    """Clear the per-thread language override."""
    if hasattr(_thread_local, 'lang'):
        del _thread_local.lang


def detect_language(text: str) -> str:
    """Detect language from input text.

    Uses character-based heuristics to determine the language:
    - If text contains CJK characters, it's likely Chinese
    - Otherwise, default to English

    Args:
        text: Input text to analyze

    Returns:
        Detected language code ('en' or 'zh')
    """
    if not text:
        return "en"

    # Count CJK (Chinese/Japanese/Korean) characters
    # Unicode ranges for CJK:
    # - CJK Unified Ideographs: U+4E00 to U+9FFF
    # - CJK Extension A: U+3400 to U+4DBF
    # - CJK Compatibility Ideographs: U+F900 to U+FAFF
    cjk_pattern = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]')
    cjk_chars = len(cjk_pattern.findall(text))

    # If more than 10% of characters are CJK, assume Chinese
    total_chars = len(text.strip())
    if total_chars > 0 and (cjk_chars / total_chars) > 0.1:
        return "zh"

    return "en"


def auto_detect_and_switch(text: str) -> str:
    """Auto-detect language from text and switch if needed.

    This function is called on every user message to automatically
    set the language to match the user's current language.

    Uses thread-local storage to avoid multi-tenant language contention:
    each thread/request gets its own language without affecting others.
    The global _current_lang is only set by explicit set_language() calls.

    Args:
        text: User input text

    Returns:
        The detected language code
    """
    detected_lang = detect_language(text)

    # Set thread-local language (takes precedence over global in get_language())
    # This isolates per-request language in multi-user scenarios (Telegram/WeChat)
    if detected_lang in _translations:
        set_thread_language(detected_lang)
    else:
        set_thread_language("en")

    return detected_lang


def _(key: str, **kwargs) -> str:
    """Translate a message key to the current language.

    Args:
        key: Message key
        **kwargs: Format arguments

    Returns:
        Translated message

    Example:
        >>> _("rate_limit_exceeded")
        'rate limit exceeded'
        >>> _("request_body_too_large", size=1000, max=500)
        'request body too large (1000 > 500)'
    """
    # Get translation for current language, fall back to English
    with _lock:
        lang = get_language()
        lang_dict = _translations.get(lang, _translations["en"])
    message = lang_dict.get(key, _translations["en"].get(key, key))

    # Format with kwargs if provided
    if kwargs:
        try:
            message = message.format(**kwargs)
        except (KeyError, ValueError) as exc:
            logger.warning("failed to format message '%s': %s", key, exc)

    return message


def add_translation(lang: str, key: str, message: str) -> None:
    """Add a custom translation.

    Args:
        lang: Language code
        key: Message key
        message: Translated message
    """
    with _lock:
        if lang not in _translations:
            _translations[lang] = {}
        _translations[lang][key] = message


def load_translations_from_dict(translations: Dict[str, Dict[str, str]]) -> None:
    """Load translations from a dictionary.

    Args:
        translations: Dict of {lang: {key: message}}
    """
    with _lock:
        for lang, messages in translations.items():
            if lang not in _translations:
                _translations[lang] = {}
            _translations[lang].update(messages)


# ============================================================================
# 增强功能：更多语言、翻译记忆、语言包管理
# ============================================================================

# 日语翻译
_ja_translations = {
    "rate_limit_exceeded": "リクエスト頻度が制限を超えています",
    "invalid_api_key": "無効なAPIキー",
    "not_ready": "準備ができていません",
    "internal_error": "内部サーバーエラー",
    "welcome": "╔══════════════════════════════════════════════╗\n║  One-Agent v2 — 自然言語で操作、「ヘルプ」と入力   ║\n╚══════════════════════════════════════════════╝",
    "timeout": "[タイムアウト — 再試行してください]",
    "shutting_down": "[シャットダウン中...]",
    "ok": "正常",
    "error": "エラー",
    "success": "成功",
    "failed": "失敗",
    "no_api_key": "[プロバイダー '{provider}' のAPIキーが設定されていません]",
    "service_unavailable": "[サービスは一時的に利用できません — 後で再試行してください]",
}

# 韩语翻译
_ko_translations = {
    "rate_limit_exceeded": "요청 빈도가 제한을 초과했습니다",
    "invalid_api_key": "유효하지 않은 API 키",
    "not_ready": "준비되지 않았습니다",
    "internal_error": "내부 서버 오류",
    "welcome": "╔══════════════════════════════════════════════╗\n║  One-Agent v2 — 자연어로 조작, '도움' 입력   ║\n╚══════════════════════════════════════════════╝",
    "timeout": "[시간 초과 — 다시 시도하세요]",
    "shutting_down": "[종료 중...]",
    "ok": "정상",
    "error": "오류",
    "success": "성공",
    "failed": "실패",
    "no_api_key": "[제공자 '{provider}'의 API 키가 설정되지 않았습니다]",
    "service_unavailable": "[서비스가 일시적으로 사용할 수 없습니다 — 나중에 다시 시도하세요]",
}

# 注册新语言
_translations["ja"] = _ja_translations
_translations["ko"] = _ko_translations


# 语言元信息
_LANGUAGE_META = {
    "en": {"name": "English", "native_name": "English", "flag": "🇺🇸", "direction": "ltr"},
    "zh": {"name": "Chinese", "native_name": "中文", "flag": "🇨🇳", "direction": "ltr"},
    "ja": {"name": "Japanese", "native_name": "日本語", "flag": "🇯🇵", "direction": "ltr"},
    "ko": {"name": "Korean", "native_name": "한국어", "flag": "🇰🇷", "direction": "ltr"},
}


def list_languages() -> List[Dict[str, str]]:
    """列出所有支持的语言。

    Returns:
        语言信息列表
    """
    with _lock:
        return [
            {
                "code": lang,
                "name": _LANGUAGE_META.get(lang, {}).get("name", lang),
                "native_name": _LANGUAGE_META.get(lang, {}).get("native_name", lang),
                "flag": _LANGUAGE_META.get(lang, {}).get("flag", ""),
                "translation_count": len(_translations.get(lang, {})),
            }
            for lang in _translations
        ]


def get_language_meta(lang: str) -> Optional[Dict[str, str]]:
    """获取语言元信息。

    Args:
        lang: 语言代码

    Returns:
        语言元信息字典
    """
    return _LANGUAGE_META.get(lang)


def detect_language_enhanced(text: str) -> str:
    """增强的语言检测（支持日韩语检测）。

    Args:
        text: 输入文本

    Returns:
        检测到的语言代码
    """
    if not text:
        return "en"

    # 检测中文字符
    cjk_pattern = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]')
    cjk_chars = len(cjk_pattern.findall(text))

    # 检测日语假名
    ja_pattern = re.compile(r'[\u3040-\u309f\u30a0-\u30ff]')
    ja_chars = len(ja_pattern.findall(text))

    # 检测韩语谚文
    ko_pattern = re.compile(r'[\uac00-\ud7af\u1100-\u11ff]')
    ko_chars = len(ko_pattern.findall(text))

    total_chars = len(text.strip())

    if total_chars == 0:
        return "en"

    # 优先级：日语假名 > 韩语谚文 > 中文 > 英文
    if ja_chars > 0 and (ja_chars / total_chars) > 0.05:
        return "ja"
    if ko_chars > 0 and (ko_chars / total_chars) > 0.05:
        return "ko"
    if cjk_chars > 0 and (cjk_chars / total_chars) > 0.1:
        return "zh"

    return "en"


class TranslationMemory:
    """翻译记忆库 — 存储和检索历史翻译。"""

    def __init__(self, data_dir: str = "data/i18n/translation_memory"):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._memory_file = self._data_dir / "memory.json"
        self._memory: Dict[str, Dict[str, str]] = {}
        self._load()

    def _load(self):
        """加载翻译记忆。"""
        if self._memory_file.exists():
            try:
                with open(self._memory_file, "r", encoding="utf-8") as f:
                    self._memory = json.load(f)
            except Exception as exc:
                logger.warning("Failed to load translation memory: %s", exc)

    def _save(self):
        """保存翻译记忆。"""
        try:
            with open(self._memory_file, "w", encoding="utf-8") as f:
                json.dump(self._memory, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning("Failed to save translation memory: %s", exc)

    def add(self, source_text: str, target_text: str, source_lang: str, target_lang: str):
        """添加翻译记忆。"""
        key = f"{source_lang}:{target_lang}:{source_text}"
        self._memory[key] = target_text
        self._save()

    def lookup(self, source_text: str, source_lang: str, target_lang: str) -> Optional[str]:
        """查找翻译记忆。"""
        key = f"{source_lang}:{target_lang}:{source_text}"
        return self._memory.get(key)

    def fuzzy_lookup(self, source_text: str, source_lang: str, target_lang: str, threshold: float = 0.8) -> List[Dict[str, Any]]:
        """模糊查找翻译记忆。"""
        results = []
        source_lower = source_text.lower()

        for key, target_text in self._memory.items():
            parts = key.split(":", 2)
            if len(parts) < 3:
                continue
            src_lang, tgt_lang, src_text = parts

            if src_lang == source_lang and tgt_lang == target_lang:
                # 简单相似度计算
                src_lower = src_text.lower()
                if src_lower == source_lower:
                    similarity = 1.0
                else:
                    common_chars = len(set(src_lower) & set(source_lower))
                    max_chars = max(len(src_lower), len(source_lower))
                    similarity = common_chars / max_chars if max_chars > 0 else 0

                if similarity >= threshold:
                    results.append({
                        "source": src_text,
                        "target": target_text,
                        "similarity": similarity,
                    })

        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:10]

    def get_stats(self) -> Dict[str, Any]:
        """获取翻译记忆统计。"""
        lang_pairs = {}
        for key in self._memory:
            parts = key.split(":", 2)
            if len(parts) >= 2:
                pair = f"{parts[0]}→{parts[1]}"
                lang_pairs[pair] = lang_pairs.get(pair, 0) + 1

        return {
            "total_entries": len(self._memory),
            "language_pairs": lang_pairs,
        }


class LanguagePackManager:
    """语言包管理器 — 管理语言包的安装和更新。"""

    def __init__(self, packs_dir: str = "data/i18n/packs"):
        self._packs_dir = Path(packs_dir)
        self._packs_dir.mkdir(parents=True, exist_ok=True)

    def list_packs(self) -> List[Dict[str, Any]]:
        """列出已安装的语言包。"""
        packs = []
        if self._packs_dir.exists():
            for pack_file in self._packs_dir.glob("*.json"):
                try:
                    with open(pack_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    packs.append({
                        "lang": data.get("lang", pack_file.stem),
                        "name": data.get("name", pack_file.stem),
                        "version": data.get("version", "1.0.0"),
                        "author": data.get("author", "unknown"),
                        "translation_count": len(data.get("translations", {})),
                    })
                except Exception:
                    pass
        return packs

    def install_pack(self, pack_data: Dict[str, Any]) -> bool:
        """安装语言包。"""
        try:
            lang = pack_data["lang"]
            translations = pack_data.get("translations", {})

            # 保存语言包文件
            pack_file = self._packs_dir / f"{lang}.json"
            with open(pack_file, "w", encoding="utf-8") as f:
                json.dump(pack_data, f, indent=2, ensure_ascii=False)

            # 加载翻译
            with _lock:
                if lang not in _translations:
                    _translations[lang] = {}
                _translations[lang].update(translations)

            # 更新语言元信息
            if "meta" in pack_data:
                _LANGUAGE_META[lang] = pack_data["meta"]

            logger.info("Language pack installed: %s", lang)
            return True
        except Exception as exc:
            logger.warning("Failed to install language pack: %s", exc)
            return False

    def uninstall_pack(self, lang: str) -> bool:
        """卸载语言包（仅卸载用户安装的）。"""
        if lang in {"en", "zh", "ja", "ko"}:
            return False  # 内置语言不可卸载

        pack_file = self._packs_dir / f"{lang}.json"
        if pack_file.exists():
            pack_file.unlink()

        with _lock:
            if lang in _translations:
                del _translations[lang]

        if lang in _LANGUAGE_META:
            del _LANGUAGE_META[lang]

        logger.info("Language pack uninstalled: %s", lang)
        return True

    def export_pack(self, lang: str, output_path: str) -> bool:
        """导出语言包。"""
        try:
            with _lock:
                translations = _translations.get(lang, {}).copy()

            meta = _LANGUAGE_META.get(lang, {})
            pack_data = {
                "lang": lang,
                "name": meta.get("name", lang),
                "meta": meta,
                "version": "1.0.0",
                "author": "exported",
                "translations": translations,
            }

            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(pack_data, f, indent=2, ensure_ascii=False)

            return True
        except Exception as exc:
            logger.warning("Failed to export language pack: %s", exc)
            return False


class I18nManager:
    """国际化管理器 — 整合所有i18n功能。"""

    def __init__(self, data_dir: str = "data/i18n"):
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._translation_memory = TranslationMemory(str(self._data_dir / "memory"))
        self._pack_manager = LanguagePackManager(str(self._data_dir / "packs"))

    @property
    def translation_memory(self) -> TranslationMemory:
        """获取翻译记忆库。"""
        return self._translation_memory

    @property
    def pack_manager(self) -> LanguagePackManager:
        """获取语言包管理器。"""
        return self._pack_manager

    def get_current_language(self) -> str:
        """获取当前语言。"""
        return get_language()

    def set_language(self, lang: str) -> bool:
        """设置当前语言。"""
        set_language(lang)
        return True

    def translate(self, key: str, **kwargs) -> str:
        """翻译。"""
        return _(key, **kwargs)

    def list_languages(self) -> List[Dict[str, str]]:
        """列出所有语言。"""
        return list_languages()

    def detect_language(self, text: str) -> str:
        """检测语言。"""
        return detect_language_enhanced(text)

    def get_stats(self) -> Dict[str, Any]:
        """获取国际化统计信息。"""
        return {
            "current_language": get_language(),
            "supported_languages": len(_translations),
            "translation_memory": self._translation_memory.get_stats(),
            "installed_packs": len(self._pack_manager.list_packs()),
        }
