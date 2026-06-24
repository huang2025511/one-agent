"""Dialog Summary Memory — automatic conversation summarization.

Long conversations get summarized and stored in long-term memory so:
- Earlier context isn't lost when history rolls off
- Future conversations can reference past discussions
- Memory stays efficient (summaries are compact)

Features:
- Auto-summarize every N turns (configurable)
- Progressive summarization (merge new summary with previous)
- Store summaries in long-term memory with semantic embeddings
- Recall relevant summaries for new conversations
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_SUMMARY_INTERVAL = 10  # Summarize every N turns
MAX_SUMMARY_LENGTH = 800  # Max chars per summary


class DialogSummarizer:
    """Automatic conversation summarizer with progressive updates.

    Summaries are stored as structured entries:
    {
        "session_id": "...",
        "summary": "...",
        "turn_count": 42,
        "topics": ["topic1", "topic2"],
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    """

    def __init__(
        self,
        summary_interval: int = DEFAULT_SUMMARY_INTERVAL,
        max_summary_length: int = MAX_SUMMARY_LENGTH,
    ) -> None:
        self._summary_interval = summary_interval
        self._max_summary_length = max_summary_length

        # In-memory store of summaries by session
        self._summaries: Dict[str, Dict[str, Any]] = {}
        # Turn counters per session
        self._turn_counters: Dict[str, int] = {}

    def should_summarize(self, session_id: str) -> bool:
        """Check if it's time to summarize this session."""
        count = self._turn_counters.get(session_id, 0)
        return count > 0 and count % self._summary_interval == 0

    def increment_turn(self, session_id: str) -> int:
        """Increment turn counter for a session."""
        self._turn_counters[session_id] = self._turn_counters.get(session_id, 0) + 1
        return self._turn_counters[session_id]

    def generate_summary_prompt(
        self,
        history: List[Dict[str, str]],
        existing_summary: str = "",
        lang: str = "zh",
    ) -> str:
        """Generate a prompt for LLM to create/update a conversation summary.

        Args:
            history: List of {"input": "...", "reply": "..."} entries
            existing_summary: Previous summary to merge/extend
            lang: Language for the summary

        Returns:
            Prompt string to send to LLM
        """
        if lang.startswith("zh"):
            return self._generate_prompt_zh(history, existing_summary)
        else:
            return self._generate_prompt_en(history, existing_summary)

    def _generate_prompt_zh(
        self, history: List[Dict[str, str]], existing_summary: str
    ) -> str:
        parts = ["请生成这段对话的简洁摘要。\n"]

        if existing_summary:
            parts.append(
                f"之前的对话摘要（请在这个基础上更新，不要重复已有内容）：\n"
                f"{existing_summary}\n\n"
            )

        parts.append("最近的对话内容：\n")
        for i, msg in enumerate(history[-self._summary_interval:]):
            parts.append(f"[用户] {msg.get('input', '')[:300]}")
            if msg.get("reply"):
                parts.append(f"[助手] {msg['reply'][:300]}")
            parts.append("")

        parts.append(
            "\n要求：\n"
            f"1. 摘要不超过 {self._max_summary_length} 字\n"
            "2. 只保留重要信息：关键问题、决策、结论、待办事项\n"
            "3. 用简洁的要点式或段落式表达\n"
            "4. 如果有之前的摘要，请合并新内容，保持连贯\n"
            "5. 直接输出摘要，不要解释或加标题"
        )

        return "\n".join(parts)

    def _generate_prompt_en(
        self, history: List[Dict[str, str]], existing_summary: str
    ) -> str:
        parts = ["Generate a concise summary of this conversation.\n"]

        if existing_summary:
            parts.append(
                f"Previous summary (update from this, don't repeat):\n"
                f"{existing_summary}\n\n"
            )

        parts.append("Recent conversation:\n")
        for i, msg in enumerate(history[-self._summary_interval:]):
            parts.append(f"[User] {msg.get('input', '')[:300]}")
            if msg.get("reply"):
                parts.append(f"[Assistant] {msg['reply'][:300]}")
            parts.append("")

        parts.append(
            "\nRequirements:\n"
            f"1. Keep summary under {self._max_summary_length} chars\n"
            "2. Keep only important info: key questions, decisions, conclusions, action items\n"
            "3. Use concise bullet points or paragraphs\n"
            "4. Merge with previous summary if provided\n"
            "5. Output only the summary, no explanations or titles"
        )

        return "\n".join(parts)

    def store_summary(
        self,
        session_id: str,
        summary: str,
        turn_count: int,
        topics: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Store/update a conversation summary."""
        now = time.time()
        entry = {
            "session_id": session_id,
            "summary": summary,
            "turn_count": turn_count,
            "topics": topics or [],
            "created_at": self._summaries.get(session_id, {}).get("created_at", now),
            "updated_at": now,
        }
        self._summaries[session_id] = entry
        logger.debug(
            "Summary stored for session %s (%d turns, %d chars)",
            session_id,
            turn_count,
            len(summary),
        )
        return entry

    def get_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get the latest summary for a session."""
        return self._summaries.get(session_id)

    def get_all_summaries(self) -> List[Dict[str, Any]]:
        """Get all stored summaries, sorted by update time."""
        return sorted(
            list(self._summaries.values()),
            key=lambda x: x["updated_at"],
            reverse=True,
        )

    def format_summary_for_context(
        self,
        session_id: str,
        lang: str = "zh",
    ) -> Optional[str]:
        """Format a summary for injection into conversation context.

        Returns None if no summary exists.
        """
        entry = self._summaries.get(session_id)
        if not entry:
            return None

        if lang.startswith("zh"):
            return (
                "【对话摘要】（之前的对话回顾）\n"
                f"{entry['summary']}\n"
                f"（共 {entry['turn_count']} 轮对话，更新于 {time.strftime('%H:%M', time.localtime(entry['updated_at']))}）"
            )
        else:
            return (
                "[Conversation Summary] (previous context)\n"
                f"{entry['summary']}\n"
                f"({entry['turn_count']} turns, updated at {time.strftime('%H:%M', time.localtime(entry['updated_at']))})"
            )

    def extract_topics(self, summary: str) -> List[str]:
        """Extract topic keywords from a summary (simple keyword-based).

        In production this could use NLP; for now we use simple patterns.
        """
        topics = []
        # Simple topic detection patterns
        patterns = [
            ("编程", r"代码|编程|函数|类|算法|python|java|javascript"),
            ("搜索", r"搜索|查找|查询|search"),
            ("文档", r"文档|文件|file|document"),
            ("系统", r"系统|shell|命令|system"),
            ("学习", r"学习|教程|解释|learn|tutorial"),
            ("调试", r"bug|错误|调试|debug|error"),
            ("数据", r"数据|分析|统计|data|analysis"),
            ("设计", r"设计|架构|方案|design|architecture"),
        ]

        summary_lower = summary.lower()
        for topic, pattern in patterns:
            import re
            if re.search(pattern, summary_lower):
                topics.append(topic)

        return topics[:5]

    def clear_session(self, session_id: str) -> None:
        """Clear summary for a session."""
        self._summaries.pop(session_id, None)
        self._turn_counters.pop(session_id, None)

    def stats(self) -> Dict[str, Any]:
        """Get summarizer statistics."""
        return {
            "active_sessions": len(self._summaries),
            "total_summaries": len(self._summaries),
            "summary_interval": self._summary_interval,
        }


# Singleton
_summarizer: Optional[DialogSummarizer] = None


def get_dialog_summarizer() -> DialogSummarizer:
    """Get the shared DialogSummarizer instance."""
    global _summarizer
    if _summarizer is None:
        _summarizer = DialogSummarizer()
    return _summarizer
