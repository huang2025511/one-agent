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

logger = logging.getLogger(__name__)


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
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
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
        self._failures.append(case)

        # Keep only last 100 in memory
        if len(self._failures) > 100:
            self._failures = self._failures[-100:]

        # Persist
        with self._write_lock:
            self._conn.execute(
                "INSERT INTO failures (user_input, error_type, error_detail, turn_meta, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_input[:500], error_type, error_detail[:500],
                 json.dumps(turn_meta or {}, ensure_ascii=False), time.time())
            )
            self._conn.commit()

    def analyze_patterns(self) -> List[Dict[str, Any]]:
        """Analyze recent failures to find patterns."""
        patterns = []

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
        """Generate a system prompt improvement based on failure patterns."""
        # Simple template-based improvements
        templates = {
            "frequent_error": "当工具返回错误时，不要重复调用同一工具，立即尝试替代方案。",
            "empty_result": "如果搜索结果为空，尝试用不同的关键词重新搜索，或直接基于已有知识回答。",
            "timeout": "如果某个操作耗时较长，先告知用户正在处理中，不要静默等待。",
            "llm_error": "如果 LLM 调用失败，自动降级到更简单的问题表述重试。",
        }
        return templates.get(pattern)

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
        cur = self._conn.execute(
            "SELECT * FROM improvements ORDER BY created_at DESC LIMIT 20"
        )
        return [dict(r) for r in cur.fetchall()]

    def get_stats(self) -> Dict[str, Any]:
        """Get improvement statistics."""
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

    # -------------------------------------------------------- async wrappers

    async def record_failure_async(
        self, user_input: str, error_type: str,
        error_detail: str, turn_meta: Optional[dict] = None
    ) -> None:
        """Async wrapper for record_failure"""
        await asyncio.to_thread(self.record_failure, user_input, error_type, error_detail, turn_meta)

    async def analyze_patterns_async(self) -> List[Dict[str, Any]]:
        """Async wrapper for analyze_patterns"""
        return await asyncio.to_thread(self.analyze_patterns)

    async def apply_improvement_async(self, pattern: str, suggestion: str) -> None:
        """Async wrapper for apply_improvement"""
        await asyncio.to_thread(self.apply_improvement, pattern, suggestion)

    async def get_improvements_async(self) -> List[Dict[str, Any]]:
        """Async wrapper for get_improvements"""
        return await asyncio.to_thread(self.get_improvements)

    async def get_stats_async(self) -> Dict[str, Any]:
        """Async wrapper for get_stats"""
        return await asyncio.to_thread(self.get_stats)

    async def get_failures_async(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Async wrapper for get_failures"""
        return await asyncio.to_thread(self.get_failures, limit)
