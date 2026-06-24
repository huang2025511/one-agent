"""Suggestion Engine — proactive recommendations based on context and user profile.

Provides intelligent suggestions:
- Skill recommendations based on user history and current context
- Next-step suggestions after completing a task
- Related topic suggestions from conversation history
- Proactive tips for common workflows

Suggestions are generated at turn completion and can be:
- Displayed to user (optional, configurable)
- Injected into context for LLM to consider
- Logged for analytics
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from memory.user_profile import get_profile_store

logger = logging.getLogger(__name__)


# Skill → related skills mapping (for recommendations)
_SKILL_RELATIONS = {
    "web_search": ["document_search", "transcribe"],
    "document_search": ["web_search", "python_execute"],
    "python_execute": ["calc", "document_search"],
    "transcribe": ["describe_image", "web_search"],
    "describe_image": ["transcribe", "web_search"],
    "calc": ["python_execute"],
    "save_note": ["history", "clear"],
    "history": ["clear", "save_note"],
    "system_run": ["status", "settings"],
    "settings": ["status", "system_run"],
}

# Topic → skill suggestions
_TOPIC_SKILL_MAP = {
    "代码": ["python_execute", "web_search"],
    "编程": ["python_execute", "web_search"],
    "搜索": ["web_search", "document_search"],
    "文档": ["document_search", "web_search"],
    "计算": ["calc", "python_execute"],
    "图片": ["describe_image", "web_search"],
    "音频": ["transcribe", "web_search"],
    "笔记": ["save_note", "history"],
    "系统": ["system_run", "status"],
    "设置": ["settings", "status"],
    "天气": ["web_search"],
    "新闻": ["web_search"],
    "翻译": ["web_search"],
}

# Patterns that suggest next actions
_NEXT_ACTION_PATTERNS = [
    (r"成功|完成|已.*好|done|success|finished", "ask_followup"),
    (r"错误|失败|error|failed|问题", "suggest_debug"),
    (r"代码|code|script|程序", "suggest_execute"),
    (r"搜索|search|查找|find", "suggest_summarize"),
    (r"文件|file|文档|document", "suggest_analyze"),
]


class SuggestionEngine:
    """Generates proactive suggestions based on context and user profile."""

    def __init__(self) -> None:
        self._profile = get_profile_store()
        self._recent_suggestions: List[str] = []  # Avoid repeating

    def generate_suggestions(
        self,
        user_input: str,
        assistant_reply: str,
        skills_used: List[str],
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Generate suggestions for the current turn.

        Returns a list of suggestion dicts:
        [
            {"type": "skill", "skill": "python_execute", "reason": "你经常在搜索后执行代码"},
            {"type": "next_step", "action": "保存笔记", "reason": "这个结果值得保存"},
            {"type": "tip", "message": "试试用 /help 查看所有技能", "reason": "新用户引导"},
        ]
        """
        suggestions: List[Dict[str, Any]] = []

        # 1. Skill recommendations based on history
        skill_suggestions = self._suggest_related_skills(skills_used)
        suggestions.extend(skill_suggestions)

        # 2. Skill recommendations based on topics
        topic_suggestions = self._suggest_skills_from_topics(user_input)
        suggestions.extend(topic_suggestions)

        # 3. Next-step suggestions based on reply patterns
        next_step = self._suggest_next_step(user_input, assistant_reply)
        if next_step:
            suggestions.append(next_step)

        # 4. Personalized suggestions based on user profile
        personal_suggestions = self._suggest_from_profile()
        suggestions.extend(personal_suggestions)

        # 5. Filter duplicates and limit
        suggestions = self._filter_and_limit(suggestions)

        return suggestions

    def _suggest_related_skills(self, skills_used: List[str]) -> List[Dict[str, Any]]:
        """Suggest skills related to those just used."""
        suggestions = []
        for skill in skills_used:
            related = _SKILL_RELATIONS.get(skill, [])
            for r_skill in related:
                # Check if user has used this skill before
                top_skills = [s[0] for s in self._profile.get_top_skills(10)]
                if r_skill in top_skills:
                    suggestions.append({
                        "type": "skill",
                        "skill": r_skill,
                        "reason": f"你经常在 {skill} 之后使用 {r_skill}",
                        "priority": "high",
                    })
                else:
                    suggestions.append({
                        "type": "skill",
                        "skill": r_skill,
                        "reason": f"{skill} 的相关技能",
                        "priority": "medium",
                    })
        return suggestions

    def _suggest_skills_from_topics(self, user_input: str) -> List[Dict[str, Any]]:
        """Suggest skills based on detected topics in user input."""
        suggestions = []
        for topic, skills in _TOPIC_SKILL_MAP.items():
            if topic in user_input:
                for skill in skills:
                    suggestions.append({
                        "type": "skill",
                        "skill": skill,
                        "reason": f"话题 '{topic}' 相关",
                        "priority": "medium",
                    })
        return suggestions

    def _suggest_next_step(
        self,
        user_input: str,
        assistant_reply: str,
    ) -> Optional[Dict[str, Any]]:
        """Suggest a next action based on reply patterns."""
        combined = user_input + assistant_reply
        for pattern, action_type in _NEXT_ACTION_PATTERNS:
            if re.search(pattern, combined, re.IGNORECASE):
                if action_type == "ask_followup":
                    return {
                        "type": "next_step",
                        "action": "继续探索",
                        "reason": "任务已完成，可以继续深入",
                        "priority": "low",
                    }
                elif action_type == "suggest_debug":
                    return {
                        "type": "next_step",
                        "action": "调试问题",
                        "reason": "检测到错误，建议排查",
                        "priority": "high",
                    }
                elif action_type == "suggest_execute":
                    return {
                        "type": "next_step",
                        "action": "运行代码",
                        "reason": "代码已生成，可以执行测试",
                        "priority": "high",
                    }
                elif action_type == "suggest_summarize":
                    return {
                        "type": "next_step",
                        "action": "总结要点",
                        "reason": "搜索结果较多，建议总结",
                        "priority": "medium",
                    }
                elif action_type == "suggest_analyze":
                    return {
                        "type": "next_step",
                        "action": "分析文档",
                        "reason": "文档已找到，可以深入分析",
                        "priority": "medium",
                    }
        return None

    def _suggest_from_profile(self) -> List[Dict[str, Any]]:
        """Generate suggestions based on user profile."""
        suggestions = []

        # Suggest frequently used skills that haven't been used recently
        top_skills = self._profile.get_top_skills(5)
        recent_topics = self._profile.get_recent_topics(24)

        # If user has a preferred language, suggest switching if different
        lang = self._profile.get_preference("language")
        if lang and lang != "zh":
            suggestions.append({
                "type": "tip",
                "message": f"你偏好使用 {lang}，可以随时切换",
                "reason": "个性化提示",
                "priority": "low",
            })

        # Suggest based on active hours
        active_hours = self._profile.get_active_hours()
        current_hour = time.localtime().tm_hour
        if active_hours and current_hour in active_hours:
            suggestions.append({
                "type": "tip",
                "message": "这是你常用的活跃时段，效率最佳",
                "reason": "时间模式提示",
                "priority": "low",
            })

        return suggestions

    def _filter_and_limit(
        self,
        suggestions: List[Dict[str, Any]],
        limit: int = 3,
    ) -> List[Dict[str, Any]]:
        """Filter duplicates and limit suggestions."""
        # Remove duplicates by skill/action
        seen = set()
        filtered = []
        for s in suggestions:
            key = s.get("skill") or s.get("action") or s.get("message")
            if key and key not in seen and key not in self._recent_suggestions:
                seen.add(key)
                filtered.append(s)

        # Sort by priority
        priority_order = {"high": 0, "medium": 1, "low": 2}
        filtered.sort(key=lambda x: priority_order.get(x.get("priority", "medium"), 1))

        # Limit
        result = filtered[:limit]

        # Track recent suggestions to avoid repetition
        for s in result:
            key = s.get("skill") or s.get("action") or s.get("message")
            if key:
                self._recent_suggestions.append(key)
        # Keep only last 10
        self._recent_suggestions = self._recent_suggestions[-10:]

        return result

    def format_suggestions_for_display(
        self,
        suggestions: List[Dict[str, Any]],
    ) -> str:
        """Format suggestions for user display."""
        if not suggestions:
            return ""

        lines = ["\n💡 建议："]
        for s in suggestions:
            if s["type"] == "skill":
                lines.append(f"  • 试试 `{s['skill']}` — {s['reason']}")
            elif s["type"] == "next_step":
                lines.append(f"  • 下一步：{s['action']} — {s['reason']}")
            elif s["type"] == "tip":
                lines.append(f"  • {s['message']}")

        return "\n".join(lines)

    def format_suggestions_for_llm(
        self,
        suggestions: List[Dict[str, Any]],
    ) -> str:
        """Format suggestions for LLM context injection."""
        if not suggestions:
            return ""

        lines = ["[系统建议 - 可参考但不必执行]"]
        for s in suggestions:
            if s["type"] == "skill":
                lines.append(f"- 用户可能需要技能: {s['skill']} ({s['reason']})")
            elif s["type"] == "next_step":
                lines.append(f"- 建议下一步: {s['action']} ({s['reason']})")
            elif s["type"] == "tip":
                lines.append(f"- 提示: {s['message']}")

        return "\n".join(lines)


# Singleton
_suggestion_engine: Optional[SuggestionEngine] = None


def get_suggestion_engine() -> SuggestionEngine:
    """Get the shared SuggestionEngine instance."""
    global _suggestion_engine
    if _suggestion_engine is None:
        _suggestion_engine = SuggestionEngine()
    return _suggestion_engine