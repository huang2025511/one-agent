"""Structured tool result model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

_VALID_STATUSES = frozenset({"success", "error", "timeout", "unavailable", "skipped"})


@dataclass
class ToolResult:
    """Structured result from a skill/tool execution."""
    tool_name: str
    status: str = "success"  # success | error | timeout | unavailable | skipped
    data: Optional[Union[Dict[str, Any], str]] = None
    error: Optional[str] = None
    tokens_used: int = 0
    duration_ms: float = 0.0
    truncated: bool = False

    def __post_init__(self):
        """Validate field types and values."""
        if self.status not in _VALID_STATUSES:
            raise ValueError(
                f'Invalid status: {self.status!r}. '
                f'Must be one of: {", ".join(sorted(_VALID_STATUSES))}'
            )
        if self.data is not None and not isinstance(self.data, (dict, str)):
            raise ValueError(
                f'data must be a dict, str, or None, got {type(self.data).__name__}'
            )

    # -- backward-compat: allow ToolResult to quack like a string ----------
    def __str__(self) -> str:
        return self.to_message()

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.data == other
        return super().__eq__(other)

    def __hash__(self) -> int:
        return hash((self.tool_name, self.status, str(self.data), self.error))

    def __contains__(self, item: str) -> bool:
        return item in self.to_message()

    def __getitem__(self, key):
        return self.to_message()[key]

    def lower(self) -> str:
        return self.to_message().lower()
    # --------------------------------------------------------------------

    def to_message(self) -> str:
        """Format as a structured message for LLM consumption."""
        if self.status == "success":
            header = f"[{self.tool_name} 执行成功"
            if self.duration_ms > 0:
                header += f" ({self.duration_ms:.0f}ms)"
            if self.tokens_used > 0:
                header += f", {self.tokens_used} tokens"
            header += "]"
            body = str(self.data) if self.data else "(empty result)"
            if self.truncated:
                body += "\n[... 结果已截断]"
            return f"{header}\n{body}"
        elif self.status == "error":
            return f"[{self.tool_name} 执行失败] {self.error or '未知错误'}"
        elif self.status == "unavailable":
            return f"[{self.tool_name} 不可用] {self.error or '该工具当前不可用，请尝试其他方法'}"
        elif self.status == "skipped":
            return f"[{self.tool_name} 已跳过] {self.error or ''}"
        elif self.status == "timeout":
            # timeout 状态没有专门分支时会 fallthrough 到 `str(self.data)`，
            # 当 data is None 时返回字符串 "None" 被 LLM 当真值，造成混淆。
            return f"[{self.tool_name} 执行超时] {self.error or '工具调用超过最大执行时间'}"
        return str(self.data) if self.data is not None else "(无结果)"

    def to_dict(self) -> dict:
        """Serialize for API responses."""
        return {
            "tool_name": self.tool_name,
            "status": self.status,
            "data": str(self.data)[:500] if self.data else None,
            "error": self.error,
            "tokens_used": self.tokens_used,
            "duration_ms": self.duration_ms,
        }
