"""Custom exception hierarchy for One-Agent.

All One-Agent specific exceptions derive from ``OneAgentError`` so callers
can catch the base class when they want a blanket handler, or catch a
specific subclass for fine-grained error handling.
"""

from __future__ import annotations


class OneAgentError(Exception):
    """Base exception for all One-Agent errors."""
    pass


class InputValidationError(OneAgentError):
    """Raised when user-supplied input fails validation."""
    pass


class SecurityError(OneAgentError):
    """Raised when a security policy blocks an operation."""
    pass
