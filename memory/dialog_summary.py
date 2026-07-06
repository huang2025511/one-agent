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


class TaskState:
    """Gap 修复：结构化任务状态跟踪。

    之前 DialogSummarizer 只做摘要，没有"当前活跃任务"的概念。
    用户说"把刚才那个改一下"时，agent 需要从对话历史里找，而不是从状态里取。

    现在跟踪每个 session 的活跃任务列表、进度、待办事项。
    """

    def __init__(self) -> None:
        self.name: str = ""  # 任务名称
        self.status: str = "pending"  # pending / in_progress / completed
        self.steps: List[Dict[str, Any]] = []  # [{step, status, result}]
        self.created_at: float = time.time()
        self.updated_at: float = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "steps": self.steps,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_context_string(self, lang: str = "zh") -> str:
        """格式化为可注入对话上下文的字符串。"""
        if not self.name:
            return ""
        if lang.startswith("zh"):
            status_map = {"pending": "待开始", "in_progress": "进行中", "completed": "已完成"}
            lines = [
                f"【当前任务】{self.name} ({status_map.get(self.status, self.status)})",
            ]
            if self.steps:
                lines.append("步骤进度：")
                for i, s in enumerate(self.steps):
                    status_icon = "✅" if s.get("status") == "done" else "⏳" if s.get("status") == "in_progress" else "⬜"
                    lines.append(f"  {status_icon} 步骤{i+1}: {s.get('step', '')}")
            return "\n".join(lines)
        else:
            lines = [
                f"[Active Task] {self.name} ({self.status})",
            ]
            if self.steps:
                lines.append("Steps:")
                for i, s in enumerate(self.steps):
                    status_icon = "✅" if s.get("status") == "done" else "⏳" if s.get("status") == "in_progress" else "⬜"
                    lines.append(f"  {status_icon} Step {i+1}: {s.get('step', '')}")
            return "\n".join(lines)


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
        # Gap 修复：结构化任务状态跟踪（per session）
        self._active_tasks: Dict[str, TaskState] = {}
        self._completed_tasks: Dict[str, List[TaskState]] = {}

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
        self._active_tasks.pop(session_id, None)
        self._completed_tasks.pop(session_id, None)

    # ---- Gap 修复：结构化任务状态跟踪 ----

    def set_active_task(self, session_id: str, task_name: str, steps: Optional[List[str]] = None) -> TaskState:
        """设置或更新当前活跃任务。"""
        task = self._active_tasks.get(session_id)
        if task is None:
            task = TaskState()
            self._active_tasks[session_id] = task
        task.name = task_name
        task.status = "in_progress"
        task.updated_at = time.time()
        if steps:
            task.steps = [{"step": s, "status": "pending", "result": ""} for s in steps]
        return task

    def update_task_step(self, session_id: str, step_index: int, status: str, result: str = "") -> Optional[TaskState]:
        """更新任务步骤状态。"""
        task = self._active_tasks.get(session_id)
        if task is None or step_index >= len(task.steps):
            return None
        task.steps[step_index]["status"] = status
        if result:
            task.steps[step_index]["result"] = result
        task.updated_at = time.time()
        # 所有步骤完成 → 标记任务完成
        if all(s.get("status") == "done" for s in task.steps):
            task.status = "completed"
            self._completed_tasks.setdefault(session_id, []).append(task)
            self._active_tasks.pop(session_id, None)
        return task

    def complete_task(self, session_id: str) -> Optional[TaskState]:
        """标记任务完成。"""
        task = self._active_tasks.pop(session_id, None)
        if task:
            task.status = "completed"
            task.updated_at = time.time()
            self._completed_tasks.setdefault(session_id, []).append(task)
        return task

    def get_active_task(self, session_id: str) -> Optional[TaskState]:
        """获取当前活跃任务。"""
        return self._active_tasks.get(session_id)

    def get_task_context(self, session_id: str, lang: str = "zh") -> str:
        """获取任务状态上下文（用于注入对话）。"""
        task = self._active_tasks.get(session_id)
        if task is None:
            return ""
        return task.to_context_string(lang)

    def detect_topic_switch(self, session_id: str, new_text: str) -> bool:
        """启发式检测话题切换：新文本是否与当前任务无关。"""
        task = self._active_tasks.get(session_id)
        if task is None:
            return False
        # 简单关键词检测：新文本包含"换一个"、"先不管"、"新任务"等
        switch_keywords = ["换一个", "先不管", "改成", "另外", "换个话题", "新任务", "先做别的",
                           "different", "switch", "new task", "change topic"]
        text_lower = new_text.lower()[:200]
        return any(kw in text_lower for kw in switch_keywords)

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
