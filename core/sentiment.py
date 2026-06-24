"""Sentiment Analyzer — detect user emotion and adjust response style.

Detects emotions from user input:
- Frustration (着急/不耐烦)
- Confusion (困惑/不理解)
- Satisfaction (满意/感谢)
- Anger (愤怒/不满)
- Curiosity (好奇/探索)
- Neutral (中性)

Based on detected emotion, suggests response style adjustments:
- Frustration → concise, direct, actionable
- Confusion → explanatory, step-by-step, patient
- Satisfaction → brief acknowledgment, continue naturally
- Anger → apologetic, solution-focused, calm
- Curiosity → detailed, engaging, encourage exploration
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Emotion patterns (Chinese + English)
_EMOTION_PATTERNS = {
    "frustration": [
        # Chinese
        r"快点|快点啊|怎么这么慢|急|着急|快点解决|别废话|直接说|简单点|烦|烦死了|受不了|够了|别再|怎么还不",
        # English
        r"hurry|quick|fast|slow|too slow|annoying|frustrated|impatient|enough|stop|just tell|simple",
    ],
    "confusion": [
        # Chinese
        r"不懂|不明白|什么意思|怎么回事|为什么|怎么|解释一下|再说一遍|没听懂|糊涂|搞不懂|理解不了|详细说",
        # English
        r"don't understand|confused|what does|why|how|explain|again|clarify|detail|help me understand",
    ],
    "satisfaction": [
        # Chinese
        r"谢谢|感谢|太好了|很好|不错|完美|解决了|明白了|懂了|好的|ok|可以|棒|赞|厉害|牛逼",
        # English
        r"thanks|thank you|great|good|perfect|solved|understood|ok|awesome|nice|well done",
    ],
    "anger": [
        # Chinese
        r"垃圾|废物|没用|差劲|烂|破|什么破|什么垃圾|太差|太烂|投诉|举报|退款|赔偿|道歉|道歉|对不起我|不满意|差评",
        # English
        r"garbage|trash|useless|crap|sucks|terrible|complaint|refund|apologize|sorry|unacceptable",
    ],
    "curiosity": [
        # Chinese
        r"好奇|想知道|了解一下|看看|试试|探索|研究|深入|更多|还有什么|其他|别的|有趣|有意思",
        # English
        r"curious|want to know|learn|explore|try|more|other|interesting|tell me more",
    ],
}

# Response style recommendations per emotion
_RESPONSE_STYLES = {
    "frustration": {
        "style": "concise",
        "tone": "direct",
        "max_length": "short",
        "suggestions": ["直接给出答案", "避免冗长解释", "快速解决问题"],
    },
    "confusion": {
        "style": "explanatory",
        "tone": "patient",
        "max_length": "detailed",
        "suggestions": ["分步骤解释", "使用类比", "提供示例", "确认理解"],
    },
    "satisfaction": {
        "style": "brief",
        "tone": "friendly",
        "max_length": "short",
        "suggestions": ["简短确认", "继续下一步", "询问是否需要其他帮助"],
    },
    "anger": {
        "style": "apologetic",
        "tone": "calm",
        "max_length": "medium",
        "suggestions": ["承认问题", "提供解决方案", "保持冷静", "避免争辩"],
    },
    "curiosity": {
        "style": "engaging",
        "tone": "encouraging",
        "max_length": "detailed",
        "suggestions": ["提供详细信息", "鼓励探索", "提供相关资源", "引导深入"],
    },
    "neutral": {
        "style": "balanced",
        "tone": "professional",
        "max_length": "medium",
        "suggestions": ["正常响应"],
    },
}


class SentimentAnalyzer:
    """Analyzes user sentiment and suggests response style."""

    def __init__(self) -> None:
        # Compile patterns for efficiency
        self._compiled_patterns: Dict[str, List[re.Pattern]] = {}
        for emotion, patterns in _EMOTION_PATTERNS.items():
            self._compiled_patterns[emotion] = [
                re.compile(p, re.IGNORECASE) for p in patterns
            ]

        # Track recent emotions for context
        self._emotion_history: List[Tuple[str, float]] = []
        self._max_history = 10

    def analyze(self, text: str) -> Dict[str, Any]:
        """Analyze sentiment of user input.

        Returns:
        {
            "emotion": "frustration" | "confusion" | ...,
            "confidence": 0.0-1.0,
            "matched_patterns": ["pattern1", ...],
            "response_style": {...},
        }
        """
        # Score each emotion
        scores: Dict[str, float] = {}
        matched: Dict[str, List[str]] = {}

        for emotion, patterns in self._compiled_patterns.items():
            score = 0.0
            matches = []
            for pattern in patterns:
                found = pattern.findall(text)
                if found:
                    score += len(found) * 0.3  # Each match adds 0.3
                    matches.extend(found[:3])  # Keep top 3 matches
            scores[emotion] = min(score, 1.0)  # Cap at 1.0
            matched[emotion] = matches

        # Find dominant emotion
        if not scores or max(scores.values()) == 0:
            dominant = "neutral"
            confidence = 0.5
        else:
            dominant = max(scores, key=scores.get)
            confidence = scores[dominant]

        # Track history
        self._emotion_history.append((dominant, confidence))
        self._emotion_history = self._emotion_history[-self._max_history:]

        # Get response style recommendation
        style = _RESPONSE_STYLES.get(dominant, _RESPONSE_STYLES["neutral"])

        return {
            "emotion": dominant,
            "confidence": confidence,
            "matched_patterns": matched.get(dominant, []),
            "response_style": style,
        }

    def get_emotion_trend(self) -> Optional[str]:
        """Get the trend of recent emotions (improving/worsening/stable)."""
        if len(self._emotion_history) < 3:
            return None

        recent = [e[0] for e in self._emotion_history[-3:]]
        # Map emotions to scores (positive/negative)
        emotion_scores = {
            "satisfaction": 2,
            "curiosity": 1,
            "neutral": 0,
            "confusion": -1,
            "frustration": -2,
            "anger": -3,
        }

        scores = [emotion_scores.get(e, 0) for e in recent]
        if scores[-1] > scores[0]:
            return "improving"
        elif scores[-1] < scores[0]:
            return "worsening"
        else:
            return "stable"

    def get_context_adjustment(self) -> Dict[str, Any]:
        """Get context adjustment based on emotion history."""
        trend = self.get_emotion_trend()
        recent_emotions = [e[0] for e in self._emotion_history[-5:]]

        adjustment = {
            "trend": trend,
            "recent_emotions": recent_emotions,
            "suggestion": None,
        }

        # If user has been confused/frustrated multiple times
        negative_count = sum(
            1 for e in recent_emotions if e in ("confusion", "frustration", "anger")
        )
        if negative_count >= 3:
            adjustment["suggestion"] = "用户持续困惑/不满，建议主动询问是否需要详细解释或换个方式"

        # If user has been satisfied multiple times
        positive_count = sum(
            1 for e in recent_emotions if e in ("satisfaction", "curiosity")
        )
        if positive_count >= 3:
            adjustment["suggestion"] = "用户状态良好，可以继续深入探索或提供更多选择"

        return adjustment

    def format_for_llm(self, analysis: Dict[str, Any]) -> str:
        """Format sentiment analysis for LLM context."""
        emotion = analysis["emotion"]
        confidence = analysis["confidence"]
        style = analysis["response_style"]

        if emotion == "neutral" and confidence < 0.3:
            return ""  # Don't inject neutral/low-confidence

        lines = [
            f"[用户情绪检测: {emotion} (置信度: {confidence:.0%})]",
            f"[建议响应风格: {style['style']}, 语调: {style['tone']}]",
        ]

        if style["suggestions"]:
            lines.append(f"[建议: {', '.join(style['suggestions'][:2])}]")

        return "\n".join(lines)


# Singleton
_sentiment_analyzer: Optional[SentimentAnalyzer] = None


def get_sentiment_analyzer() -> SentimentAnalyzer:
    """Get the shared SentimentAnalyzer instance."""
    global _sentiment_analyzer
    if _sentiment_analyzer is None:
        _sentiment_analyzer = SentimentAnalyzer()
    return _sentiment_analyzer