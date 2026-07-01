"""Response Style Personalization — adapt replies to user preferences.

Adjusts response style based on user profile and learned preferences:
- Verbosity (concise / detailed / verbose)
- Tone (formal / casual / friendly / professional)
- Emoji usage (on / off / minimal)
- Code detail (minimal / balanced / thorough)
- Explanation depth (brief / step-by-step / deep)
- Analogy usage (on / off)

Works by injecting style instructions into the system prompt.
Learns from user feedback and interaction patterns.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default style profile
DEFAULT_STYLE = {
    "verbosity": "balanced",  # concise / balanced / detailed
    "tone": "friendly",  # formal / professional / friendly / casual
    "emoji": "minimal",  # on / minimal / off
    "code_detail": "balanced",  # minimal / balanced / thorough
    "explanation": "balanced",  # brief / balanced / step_by_step
    "analogy": True,  # use analogies to explain
    "language": "auto",  # auto / zh / en / ...
}

# Style presets for quick application
STYLE_PRESETS = {
    "concise_pro": {
        "verbosity": "concise",
        "tone": "professional",
        "emoji": "off",
        "code_detail": "minimal",
        "explanation": "brief",
        "analogy": False,
    },
    "friendly_teacher": {
        "verbosity": "detailed",
        "tone": "friendly",
        "emoji": "on",
        "code_detail": "thorough",
        "explanation": "step_by_step",
        "analogy": True,
    },
    "casual_chat": {
        "verbosity": "balanced",
        "tone": "casual",
        "emoji": "on",
        "code_detail": "balanced",
        "explanation": "balanced",
        "analogy": True,
    },
}


class StyleAdapter:
    """Adapts response style based on user preferences.

    Generates style instruction strings that can be injected into system
    prompts. Learns preferences from user feedback and interaction patterns.
    """

    def __init__(self, initial_style: Optional[Dict[str, Any]] = None) -> None:
        self._style = dict(DEFAULT_STYLE)
        if initial_style:
            self._style.update(initial_style)

        # Track style adjustments for learning
        self._adjustment_history: List[Dict[str, Any]] = []

    @property
    def style(self) -> Dict[str, Any]:
        return dict(self._style)

    def set_style(self, style_updates: Dict[str, Any]) -> None:
        """Update style preferences."""
        self._style.update(style_updates)
        self._adjustment_history.append({
            "updates": style_updates,
            "timestamp": __import__("time").time(),
        })

    def apply_preset(self, preset_name: str) -> bool:
        """Apply a named style preset. Returns True if preset exists."""
        preset = STYLE_PRESETS.get(preset_name)
        if preset:
            self.set_style(preset)
            return True
        return False

    def detect_style_preferences(self, user_messages: List[str]) -> Dict[str, Any]:
        """Infer style preferences from user messages.

        Analyzes patterns in how the user writes to infer their preferred
        communication style.
        """
        preferences: Dict[str, Any] = {}

        if not user_messages:
            return preferences

        all_text = " ".join(user_messages)

        # Verbosity inference
        avg_length = sum(len(m) for m in user_messages) / len(user_messages)
        if avg_length < 30:
            preferences["verbosity"] = "concise"
        elif avg_length > 200:
            preferences["verbosity"] = "detailed"

        # Emoji preference detection
        emoji_pattern = re.compile(
            "[" "\U0001F600-\U0001F64F" "\U0001F300-\U0001F5FF" "\U0001F680-\U0001F6FF"
            "\U0001F1E0-\U0001F1FF" "]+", flags=re.UNICODE
        )
        emoji_count = len(emoji_pattern.findall(all_text))
        if emoji_count > 3:
            preferences["emoji"] = "on"
        elif emoji_count > 0:
            preferences["emoji"] = "minimal"
        else:
            preferences["emoji"] = "off"

        # Tone detection
        formal_indicators = ["请", "请问", "您好", "麻烦", "感谢您", "请问一下"]
        casual_indicators = ["嘿", "嗨", "哥们", "兄弟", "咋", "啥", "嘛", "哈哈", "笑死"]

        formal_score = sum(1 for w in formal_indicators if w in all_text)
        casual_score = sum(1 for w in casual_indicators if w in all_text)

        if formal_score > casual_score and formal_score >= 2:
            preferences["tone"] = "formal"
        elif casual_score > formal_score and casual_score >= 2:
            preferences["tone"] = "casual"

        # Code detail preference
        code_indicators = ["代码", "code", "脚本", "函数", "实现", "例子", "example"]
        code_score = sum(1 for w in code_indicators if w.lower() in all_text.lower())
        if code_score >= 3:
            preferences["code_detail"] = "thorough"
        elif code_score >= 1:
            preferences["code_detail"] = "balanced"

        # Explanation depth preference
        explanation_indicators = [
            "解释", "explain", "详细", "为什么", "原理", "怎么理解",
            "步骤", "一步一步", "step by step",
        ]
        explanation_score = sum(1 for w in explanation_indicators if w.lower() in all_text.lower())
        if explanation_score >= 2:
            preferences["explanation"] = "step_by_step"

        return preferences

    def generate_system_prompt_snippet(
        self,
        lang: str = "zh",
        include_confidence: bool = False,
    ) -> str:
        """Generate a style instruction snippet for the system prompt.

        Args:
            lang: Language of the instructions
            include_confidence: Whether to include confidence notes

        Returns:
            Style instruction string to inject into system prompt
        """
        if lang.startswith("zh"):
            return self._generate_snippet_zh(include_confidence)
        else:
            return self._generate_snippet_en(include_confidence)

    def _generate_snippet_zh(self, include_confidence: bool) -> str:
        parts = ["【回复风格】"]

        # Verbosity
        if self._style["verbosity"] == "concise":
            parts.append("- 回复简洁明了，直接给答案，不要啰嗦")
        elif self._style["verbosity"] == "detailed":
            parts.append("- 回复详细全面，充分展开说明，不要遗漏重要信息")
        else:
            parts.append("- 回复详略得当，重点突出")

        # Tone
        tone_map = {
            "formal": "- 语气正式专业",
            "professional": "- 语气专业严谨",
            "friendly": "- 语气友好亲切",
            "casual": "- 语气轻松随意",
        }
        parts.append(tone_map.get(self._style["tone"], "- 语气专业友好"))

        # Emoji
        if self._style["emoji"] == "off":
            parts.append("- 不要使用 emoji 表情符号")
        elif self._style["emoji"] == "minimal":
            parts.append("- 偶尔使用 emoji，不要过多")
        else:
            parts.append("- 适当使用 emoji 增加亲切感")

        # Code detail
        if self._style["code_detail"] == "minimal":
            parts.append("- 代码示例只给关键部分，省略冗余")
        elif self._style["code_detail"] == "thorough":
            parts.append("- 代码示例完整详细，包含注释和使用方法")
        else:
            parts.append("- 代码示例清晰完整，兼顾简洁和实用")

        # Explanation depth
        if self._style["explanation"] == "brief":
            parts.append("- 解释简洁明了，点到为止")
        elif self._style["explanation"] == "step_by_step":
            parts.append("- 解释分步骤进行，循序渐进")
        else:
            parts.append("- 解释清楚到位，便于理解")

        # Analogy
        if self._style["analogy"]:
            parts.append("- 复杂概念可以用类比来辅助理解")
        else:
            parts.append("- 不要用比喻或类比，直接说事实")

        if include_confidence:
            parts.append("- 不确定的地方要说明，不要编造")

        return "\n".join(parts) + "\n"

    def _generate_snippet_en(self, include_confidence: bool) -> str:
        parts = ["[Response Style]"]

        # Verbosity
        if self._style["verbosity"] == "concise":
            parts.append("- Be concise and direct, get to the point")
        elif self._style["verbosity"] == "detailed":
            parts.append("- Be thorough and comprehensive, don't skip details")
        else:
            parts.append("- Balanced detail level, highlight key points")

        # Tone
        tone_map = {
            "formal": "- Formal and professional tone",
            "professional": "- Professional and rigorous tone",
            "friendly": "- Friendly and approachable tone",
            "casual": "- Casual and relaxed tone",
        }
        parts.append(tone_map.get(self._style["tone"], "- Professional and friendly tone"))

        # Emoji
        if self._style["emoji"] == "off":
            parts.append("- Do not use emojis")
        elif self._style["emoji"] == "minimal":
            parts.append("- Use emojis sparingly")
        else:
            parts.append("- Use emojis appropriately for warmth")

        # Code detail
        if self._style["code_detail"] == "minimal":
            parts.append("- Minimal code examples, only key parts")
        elif self._style["code_detail"] == "thorough":
            parts.append("- Complete code examples with comments and usage")
        else:
            parts.append("- Clear code examples, balanced brevity and utility")

        # Explanation depth
        if self._style["explanation"] == "brief":
            parts.append("- Brief explanations, get to the point")
        elif self._style["explanation"] == "step_by_step":
            parts.append("- Step-by-step explanations")
        else:
            parts.append("- Clear explanations for good understanding")

        # Analogy
        if self._style["analogy"]:
            parts.append("- Use analogies for complex concepts when helpful")
        else:
            parts.append("- No analogies or metaphors, just facts")

        if include_confidence:
            parts.append("- Acknowledge uncertainty, don't make things up")

        return "\n".join(parts) + "\n"

    def adjust_from_feedback(self, feedback: str) -> Dict[str, Any]:
        """Adjust style based on user feedback.

        Parses natural language feedback like:
        - "说得太啰嗦了" → verbosity: concise
        - "能不能详细一点" → verbosity: detailed
        - "别用 emoji" → emoji: off
        - "代码太简了" → code_detail: thorough
        - "我喜欢简洁的回答" → verbosity: concise

        Returns the applied style updates.
        """
        updates: Dict[str, Any] = {}
        feedback_lower = feedback.lower()

        # Verbosity feedback
        if any(w in feedback_lower for w in ["太啰嗦", "太长了", "简洁点", "简单点", "too long", "too verbose", "be concise", "shorter"]):
            updates["verbosity"] = "concise"
        elif any(w in feedback_lower for w in ["详细一点", "再详细", "更详细", "详细点", "more detail", "more details", "elaborate"]):
            updates["verbosity"] = "detailed"

        # Emoji feedback
        if any(w in feedback_lower for w in ["别用表情", "不要表情", "不用 emoji", "别用 emoji", "不要 emoji", "去掉 emoji", "no emoji", "stop emoji"]):
            updates["emoji"] = "off"
        elif any(w in feedback_lower for w in ["多点表情", "用 emoji", "more emoji", "add emoji"]):
            updates["emoji"] = "on"

        # Tone feedback
        if any(w in feedback_lower for w in ["太正式", "formal", "stiff"]):
            updates["tone"] = "casual"
        elif any(w in feedback_lower for w in ["专业点", "professional", "more formal"]):
            updates["tone"] = "professional"

        # Code detail feedback
        if any(w in feedback_lower for w in ["代码太简", "代码太少", "more code", "complete code"]):
            updates["code_detail"] = "thorough"
        elif any(w in feedback_lower for w in ["代码太多", "代码太长", "less code"]):
            updates["code_detail"] = "minimal"

        # Explanation feedback
        if any(w in feedback_lower for w in ["一步一步", "分步骤", "step by step"]):
            updates["explanation"] = "step_by_step"
        elif any(w in feedback_lower for w in ["别解释太多", "直接说", "just answer"]):
            updates["explanation"] = "brief"

        if updates:
            self.set_style(updates)
            logger.debug("Style adjusted from feedback: %s", updates)

        return updates

    def get_style_summary(self) -> str:
        """Get a human-readable summary of current style settings."""
        style = self._style
        return (
            f"回复风格："
            f"{'简洁' if style['verbosity']=='concise' else '详细' if style['verbosity']=='detailed' else '均衡'} · "
            f"{'正式' if style['tone']=='formal' else '专业' if style['tone']=='professional' else '友好' if style['tone']=='friendly' else '随意'} · "
            f"emoji: {'开启' if style['emoji']=='on' else '少量' if style['emoji']=='minimal' else '关闭'} · "
            f"代码: {'精简' if style['code_detail']=='minimal' else '详尽' if style['code_detail']=='thorough' else '均衡'}"
        )