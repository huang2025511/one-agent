"""Self-improvement loop — learn from failures and optimize prompts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from core.db import create_sqlite_connection

logger = logging.getLogger(__name__)


def _safe_json_default(obj: Any) -> Any:
    """Fallback serializer for objects that aren't natively JSON-serializable.

    Used when serializing turn_meta which may contain ToolResult, SafetyReport,
    or other dataclass objects from various subsystems.
    """
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    return str(obj)


def _safe_json_dumps(obj: Any, **kwargs) -> str:
    """Safely serialize to JSON, falling back gracefully for non-serializable objects."""
    try:
        return json.dumps(obj, ensure_ascii=kwargs.pop("ensure_ascii", False),
                          default=_safe_json_default, **kwargs)
    except Exception:
        try:
            return json.dumps({"_raw": str(obj)[:500]}, ensure_ascii=False)
        except Exception:
            return "{}"


class FailureCase:
    """A recorded failure case for analysis."""
    def __init__(self, user_input: str, error_type: str,
                 error_detail: str, turn_meta: Optional[dict] = None):
        self.user_input = user_input
        self.error_type = error_type  # tool_error, empty_result, timeout, llm_error
        self.error_detail = error_detail
        self.turn_meta = turn_meta or {}
        self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {
            "user_input": self.user_input[:500],
            "error_type": self.error_type,
            "error_detail": self.error_detail[:500],
            "timestamp": self.timestamp,
        }


class SelfImprover:
    """Records failures, analyzes patterns, and generates improvements."""

    def __init__(self, db_path: str = "data/memory/improvements.db"):
        self._conn = create_sqlite_connection(db_path)
        # Serialize writes (see audit_log.py for rationale).
        self._write_lock = threading.Lock()
        self._failures: List[FailureCase] = []
        self._improvements: List[Dict[str, Any]] = []
        self._migrate()

    def _migrate(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_input TEXT,
                error_type TEXT,
                error_detail TEXT,
                turn_meta TEXT DEFAULT '{}',
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS improvements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT,
                suggestion TEXT,
                applied INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                created_at REAL
            );
        """)
        self._conn.commit()

    def record_failure(self, user_input: str, error_type: str,
                       error_detail: str, turn_meta: Optional[dict] = None) -> None:
        """Record a failure for later analysis."""
        case = FailureCase(user_input, error_type, error_detail, turn_meta)

        # Protect both the in-memory list and the DB write with the same lock.
        # Previously _failures was mutated outside the lock, causing data races
        # when record_failure_async ran concurrently from multiple threads.
        with self._write_lock:
            self._failures.append(case)

            # Keep only last 100 in memory
            if len(self._failures) > 100:
                self._failures = self._failures[-100:]

            # Persist
            self._conn.execute(
                "INSERT INTO failures (user_input, error_type, error_detail, turn_meta, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_input[:500], error_type, error_detail[:500],
                 _safe_json_dumps(turn_meta or {}), time.time())
            )
            self._conn.commit()

    def analyze_patterns(self) -> List[Dict[str, Any]]:
        """Analyze recent failures to find patterns."""
        patterns = []

        # All reads must hold the write lock too: sqlite3 connections with
        # check_same_thread=False are not thread-safe for concurrent access,
        # and a write (which holds the lock) may be interleaving with reads.
        with self._write_lock:
            # Pattern 1: Frequent tool errors
            cur = self._conn.execute("""
                SELECT error_type, COUNT(*) as cnt
                FROM failures
                WHERE created_at > CAST(strftime('%s','now') AS REAL) - 86400
                GROUP BY error_type
                ORDER BY cnt DESC
            """)
            for row in cur.fetchall():
                if row["cnt"] >= 3:
                    patterns.append({
                        "type": "frequent_error",
                        "error_type": row["error_type"],
                        "count": row["cnt"],
                        "suggestion": f"检测到 {row['error_type']} 类型错误频繁（{row['cnt']}次/24h），建议检查相关技能或降级策略",
                    })

            # Pattern 2: Empty results
            cur = self._conn.execute(
                "SELECT COUNT(*) as cnt FROM failures WHERE error_type = 'empty_result' AND created_at > CAST(strftime('%s','now') AS REAL) - 86400"
            )
            row = cur.fetchone()
            if row and row["cnt"] >= 3:
                patterns.append({
                    "type": "empty_results",
                    "count": row["cnt"],
                    "suggestion": "多次返回空结果，建议优化搜索策略或增加备选数据源",
                })

        return patterns

    def generate_improvement(self, pattern: str) -> Optional[str]:
        """Generate a system prompt improvement based on failure patterns.

        之前是 4 条硬编码模板，完全不分析失败内容。现在保留模板作为兜底，
        真正的 LLM 分析在 generate_improvement_async 里做。
        """
        templates = {
            "frequent_error": "当工具返回错误时，不要重复调用同一工具，立即尝试替代方案。",
            "empty_result": "如果搜索结果为空，尝试用不同的关键词重新搜索，或直接基于已有知识回答。",
            "timeout": "如果某个操作耗时较长，先告知用户正在处理中，不要静默等待。",
            "llm_error": "如果 LLM 调用失败，自动降级到更简单的问题表述重试。",
        }
        return templates.get(pattern)

    async def generate_improvement_async(
        self, llm, recent_failures: List[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """用 LLM 分析真实失败案例，提炼一条可执行的改进建议。

        之前 generate_improvement 只返回硬编码模板，完全不读失败内容 →
        学不到任何东西。现在把最近 5-10 条失败案例喂给 LLM，让它提炼出
        一条具体、可操作的 system prompt 改进（不是泛泛而谈）。
        """
        if llm is None:
            return None

        # 收集最近失败案例
        if recent_failures is None:
            recent_failures = self._recent_failures(limit=8)
        if not recent_failures:
            return None

        # 构造分析 prompt
        cases_text = ""
        for i, f in enumerate(recent_failures[:8], 1):
            cases_text += (
                f"\n案例{i}:\n"
                f"  用户输入: {f.get('user_input', '')[:200]}\n"
                f"  错误类型: {f.get('error_type', '')}\n"
                f"  错误详情: {f.get('error_detail', '')[:200]}\n"
            )

        prompt = (
            "你是 one-agent 的自我改进模块。分析以下最近失败案例，提炼一条"
            "具体、可执行的 system prompt 改进建议，让 agent 以后避免类似失败。\n\n"
            "要求：\n"
            "1. 只返回一条建议（1-2句话），不要列表不要解释\n"
            "2. 建议必须具体针对这些失败模式，不要泛泛而谈\n"
            "3. 用中文\n"
            "4. 直接返回建议文本，不要包装成 JSON 或 markdown\n\n"
            f"失败案例：{cases_text}\n\n"
            "改进建议："
        )

        try:
            result = await llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=None,
                temperature=0.3,
                max_tokens=150,
                tools=None,
                use_cache=False,
            )
            suggestion = (result.get("text") or "").strip()
            # 简单清洗：去掉可能的引号/换行
            suggestion = suggestion.strip('"\'`').strip()
            if len(suggestion) < 10:
                return None
            return suggestion
        except Exception as exc:
            logger.warning("LLM 改进生成失败，回退到模板: %s", exc)
            # 回退：根据失败类型选模板
            error_types = {f.get("error_type", "") for f in recent_failures}
            for et in error_types:
                t = self.generate_improvement(et)
                if t:
                    return t
            return None

    def _recent_failures(self, limit: int = 8) -> List[Dict[str, Any]]:
        """从 DB 读取最近的失败案例。"""
        with self._write_lock:
            cur = self._conn.execute(
                "SELECT user_input, error_type, error_detail FROM failures "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
            return [dict(row) for row in cur.fetchall()]

    def apply_improvement(self, pattern: str, suggestion: str) -> None:
        """Record an applied improvement."""
        with self._write_lock:
            self._conn.execute(
                "INSERT INTO improvements (pattern, suggestion, applied, created_at) VALUES (?, ?, 1, ?)",
                (pattern, suggestion, time.time())
            )
            self._conn.commit()
        self._improvements.append({
            "pattern": pattern,
            "suggestion": suggestion,
            "applied": True,
            "time": time.time(),
        })

    def get_improvements(self) -> List[Dict[str, Any]]:
        """Get all applied improvements."""
        with self._write_lock:
            cur = self._conn.execute(
                "SELECT * FROM improvements ORDER BY created_at DESC LIMIT 20"
            )
            return [dict(r) for r in cur.fetchall()]

    def get_active_improvements(self, limit: int = 3) -> List[str]:
        """获取最近应用的改进建议（用于注入 system prompt）。

        之前 apply_improvement 只往 DB 插行，没有任何代码读这个表去改 system prompt →
        self-improvement 是开环的。现在提供这个方法，coordinator 每轮构建 prompt 时
        调用，把最近 3 条改进作为持久化的行为指导注入。
        """
        with self._write_lock:
            cur = self._conn.execute(
                "SELECT suggestion FROM improvements "
                "WHERE applied = 1 ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
            return [row["suggestion"] for row in cur.fetchall() if row["suggestion"]]

    def get_stats(self) -> Dict[str, Any]:
        """Get improvement statistics."""
        with self._write_lock:
            total_failures = self._conn.execute("SELECT COUNT(*) FROM failures").fetchone()[0]
            recent_failures = self._conn.execute(
                "SELECT COUNT(*) FROM failures WHERE created_at > CAST(strftime('%s','now') AS REAL) - 86400"
            ).fetchone()[0]
            total_improvements = self._conn.execute("SELECT COUNT(*) FROM improvements").fetchone()[0]

        return {
            "total_failures": total_failures,
            "recent_failures_24h": recent_failures,
            "total_improvements": total_improvements,
            "patterns": self.analyze_patterns(),
        }

    def get_failures(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Get recent failure cases."""
        with self._write_lock:
            cur = self._conn.execute(
                "SELECT * FROM failures ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
            return [dict(r) for r in cur.fetchall()]

    def close(self) -> None:
        """Close the database connection."""
        try:
            if self._conn:
                self._conn.close()
                self._conn = None
        except Exception:
            pass

    def __del__(self):
        """Ensure connection is closed on garbage collection."""
        if hasattr(self, '_conn') and self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
