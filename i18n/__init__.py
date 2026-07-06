"""Internationalization (i18n) module — multi-language support.

Provides:
  - Language detection from config and user input
  - Translation function _() for user-facing messages
  - Built-in translations for Chinese and English
  - Runtime language switching
  - Auto-detection of user language from input text
"""

from __future__ import annotations

import logging
import re
import threading
import contextvars
from typing import Dict

logger = logging.getLogger(__name__)

# Lock for thread-safe access to global state
_lock = threading.RLock()

# 修复：用 contextvars.ContextVar 替换 threading.local 存储请求级语言。
# 之前 threading.local 在线程池复用时不会重置, A 用户 (zh) 处理完后同一线程
# 被分给 B 用户 (en), B 拿到 zh 文本 → 多租户语言串扰。
# ContextVar 是 async 友好的: 随 asyncio.Task 自动传播和隔离,
# 每个 task/coro 有独立的 context 副本, 不会串扰。
_current_lang_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_lang_ctx", default="en"
)

# Current language (default: English) — global default (用于手动 set_language)
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
    """Get the current language code.

    优先读 ContextVar (随 asyncio.Task 自动隔离), 回退到全局 _current_lang。
    """
    try:
        ctx_lang = _current_lang_ctx.get()
        if ctx_lang and ctx_lang != "en":
            return ctx_lang
        # ContextVar 是默认值 "en" 时, 检查全局是否有手动设置
        with _lock:
            return _current_lang if _current_lang != "en" else ctx_lang
    except LookupError:
        with _lock:
            return _current_lang


def set_thread_language(lang: str) -> None:
    """Set language for the current async context / thread.

    用 ContextVar.set() 替换 threading.local, 在 asyncio.Task 间自动隔离,
    不会在线程池复用时串扰。
    """
    if lang in _translations:
        _current_lang_ctx.set(lang)
    else:
        _current_lang_ctx.set("en")


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
