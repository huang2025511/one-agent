"""Centralized log sanitization — removes secrets from log records.

This module consolidates the three previously-duplicated log sanitization
implementations that lived in ``models/__init__.py``,
``gateways/messaging.py``, and ``api/__init__.py``. All three used the
same pre-compiled-regex + ``logging.Filter`` pattern but with slightly
different pattern sets. The merged version here is the superset, so
every previously-redacted secret class is still redacted.

Usage::

    from core.log_sanitizer import install_sensitive_info_filter
    install_sensitive_info_filter(logger)  # attach once per logger

Or, to sanitize a string directly::

    from core.log_sanitizer import sanitize_log_message
    safe = sanitize_log_message(raw)
"""

from __future__ import annotations

import logging
import re
from typing import List, Tuple

# Pre-compiled regex patterns for log sanitization (hot path — every log record).
# Compiling once at module load avoids re-compiling on every call.
# This is the superset of the patterns previously split across
# models/__init__.py, gateways/messaging.py, and api/__init__.py.
_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # --- URL query-string secrets (checked first so the URL form wins
    #     over the generic "?key=" form below) ---
    (re.compile(r'https?://[^\s]*\?key=[a-zA-Z0-9\-]+'), 'https://***?key=***'),
    (re.compile(r'https?://[^\s]*\?access_token=[a-zA-Z0-9\-_\.]+'),
     'https://***?access_token=***'),
    (re.compile(r'https?://[^\s]*webhook[^\s]*'), 'https://***webhook***'),
    # --- API keys / tokens ---
    (re.compile(r'sk-ant-[a-zA-Z0-9\-]+'), '***'),                      # Anthropic keys
    (re.compile(r'sk-[a-zA-Z0-9]{20,}'), '***'),                        # OpenAI keys
    (re.compile(r'Bearer [a-zA-Z0-9\-\.]+'), 'Bearer ***'),             # Bearer tokens
    (re.compile(r'api[_-]?key[=:]\s*["\']?[a-zA-Z0-9]{20,}["\']?',
                re.IGNORECASE), 'api_key=***'),                         # generic api_key
    (re.compile(r'bot[_-]?token[=:]\s*["\']?[a-zA-Z0-9:]+["\']?',
                re.IGNORECASE), 'bot_token=***'),                       # bot tokens
    (re.compile(r'password[=:]\s*\S+', re.IGNORECASE), 'password=***'), # passwords
    # --- Generic query-string secret params (catch-all for the rest) ---
    (re.compile(r'[?&]key=[a-zA-Z0-9\-_]+'), '?key=***'),
    (re.compile(r'[?&](secret|token|access_token|app_secret|client_secret)=[^\s&]+',
                re.IGNORECASE), '?***=***'),
]


def sanitize_log_message(msg: str) -> str:
    """Remove sensitive information from a log message string.

    Uses pre-compiled patterns for performance (called on every log record).
    Non-string inputs are returned unchanged.
    """
    if not isinstance(msg, str):
        return msg
    for pattern, replacement in _PATTERNS:
        msg = pattern.sub(replacement, msg)
    return msg


class SensitiveInfoFilter(logging.Filter):
    """``logging.Filter`` that redacts secrets from every record.

    Attached to a logger via :func:`install_sensitive_info_filter`. Only
    string ``msg`` and string ``args`` are sanitized; numeric args are
    preserved so ``%``-formatting (e.g. ``logger.info("count=%d", n)``)
    keeps working.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = sanitize_log_message(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    sanitize_log_message(arg) if isinstance(arg, str) else arg
                    for arg in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: sanitize_log_message(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
        return True


def install_sensitive_info_filter(logger: logging.Logger) -> None:
    """Attach :class:`SensitiveInfoFilter` to ``logger`` (idempotent).

    Safe to call multiple times on the same logger — duplicate filters
    are skipped so log records are not sanitized twice.
    """
    for existing in logger.filters:
        if isinstance(existing, SensitiveInfoFilter):
            return
    logger.addFilter(SensitiveInfoFilter())
