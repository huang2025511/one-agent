"""Internationalization (i18n) module — multi-language support.

Provides:
  - Language detection from config
  - Translation function _() for user-facing messages
  - Built-in translations for Chinese and English
  - Runtime language switching
"""

from __future__ import annotations

import logging
from typing import Dict

logger = logging.getLogger(__name__)

# Current language (default: English)
_current_lang = "en"

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
        
        # Model errors
        "no_api_key": "[no API key configured for provider '{provider}']",
        "service_unavailable": "[service temporarily unavailable — please retry later]",
        
        # CLI messages
        "welcome": "╔══════════════════════════════════════════════╗\n║  One-Agent v2 — 自然语言即可操作，输入 '帮助'   ║\n╚══════════════════════════════════════════════╝",
        "timeout": "[timeout — try again]",
        "shutting_down": "[shutting down...]",
        
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
        
        # Model errors
        "no_api_key": "[未配置提供商 '{provider}' 的 API 密钥]",
        "service_unavailable": "[服务暂时不可用 — 请稍后重试]",
        
        # CLI messages
        "welcome": "╔══════════════════════════════════════════════╗\n║  One-Agent v2 — 自然语言即可操作，输入 '帮助'   ║\n╚══════════════════════════════════════════════╝",
        "timeout": "[超时 — 请重试]",
        "shutting_down": "[正在关闭...]",
        
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
    global _current_lang
    if lang in _translations:
        _current_lang = lang
        logger.info("language set to: %s", lang)
    else:
        logger.warning("unsupported language: %s, falling back to English", lang)
        _current_lang = "en"


def get_language() -> str:
    """Get the current language code."""
    return _current_lang


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
    lang_dict = _translations.get(_current_lang, _translations["en"])
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
    if lang not in _translations:
        _translations[lang] = {}
    _translations[lang][key] = message


def load_translations_from_dict(translations: Dict[str, Dict[str, str]]) -> None:
    """Load translations from a dictionary.
    
    Args:
        translations: Dict of {lang: {key: message}}
    """
    for lang, messages in translations.items():
        if lang not in _translations:
            _translations[lang] = {}
        _translations[lang].update(messages)
