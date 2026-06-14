"""Structured tool result model."""

from __future__ import annotations

from typing import Any, Optional
from dataclasses import dataclass


@dataclass
class ToolResult:
    """Structured result from a skill/tool execution."""
    tool_name: str
    status: str = "success"  # success | error | unavailable | skipped
    data: Any = None
    error: Optional[str] = None
    tokens_used: int = 0
    duration_ms: float = 0.0
    truncated: bool = False

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
        return str(self.data)

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