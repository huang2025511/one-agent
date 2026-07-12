"""Context Window Optimizer — intelligent conversation compression.

When conversations get long, we need to be smart about what to keep:
- Keep recent messages (fresh context)
- Keep important decisions/facts (key information)
- Keep system instructions (rules)
- Drop redundant exchanges (follow-up clarifications)
- Summarize old segments (compress history)

Strategies:
1. Simple truncation: keep last N messages (baseline)
2. Importance-based: keep messages with key entities/facts
3. Semantic clustering: group related messages, keep one representative
4. Summary injection: replace old messages with a summary

The compressor can be used at different tiers:
- Trivial/simple: just truncate (cheap)
- Complex/expert: smart compression (more compute, better context)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Token budget thresholds
MAX_CONTEXT_TOKENS_TRIVIAL = 2000
MAX_CONTEXT_TOKENS_SIMPLE = 4000
MAX_CONTEXT_TOKENS_COMPLEX = 8000
MAX_CONTEXT_TOKENS_EXPERT = 16000

# Importance scoring patterns
IMPORTANCE_PATTERNS = {
    # High importance: decisions, facts, conclusions
    "decision": [
        r"决定|决定是|选了|选.*方案|结论是|结果是|答案是",
        r"decided|conclusion|result|answer|chosen",
    ],
    "fact": [
        r"是.*[0-9]+|约.*[0-9]+|大小.*MB|版本.*[0-9]",
        r"is.*[0-9]+|approximately|version.*[0-9]",
    ],
    "action": [
        r"做了|执行|运行|创建|修改|删除|保存|发送",
        r"did|executed|created|modified|deleted|saved|send",
    ],
    "error": [
        r"错误|失败|exception|error|fail|crash|bug",
    ],
}

# Redundancy patterns - exchanges that can be summarized
REDUNDANCY_PATTERNS = [
    r"好的|收到|ok|okay|yep|yeah",  # Acknowledgments
    r"让我.*一下|我.*一下|等.*一下",  # Hesitations
    r"这个.*吗|是不是|对不对|有没有",  # Clarifications
]


class ContextCompressor:
    """Intelligent context compression for long conversations.

    Analyzes conversation structure and compresses intelligently:
    1. Identify system messages (never drop)
    2. Score each message by importance
    3. Group related messages
    4. Apply compression strategy based on budget
    """

    def __init__(
        self,
        max_tokens: int = 8000,
        preserve_recent: int = 5,
        preserve_system: bool = True,
    ) -> None:
        self._max_tokens = max_tokens
        self._preserve_recent = preserve_recent
        self._preserve_system = preserve_system

    def compress(
        self,
        messages: List[Dict[str, Any]],
        estimated_avg_token_per_char: float = 0.4,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """Compress conversation to fit within token budget.

        Returns:
            (compressed_messages, compression_summary)
            compression_summary describes what was compressed
        """
        if not messages:
            return [], "empty conversation"

        max_chars = int(self._max_tokens / estimated_avg_token_per_char)

        # Calculate current size
        current_size = sum(len(m.get("content", "")) for m in messages)

        if current_size <= max_chars:
            return list(messages), "no compression needed"

        # Strategy: importance-based compression
        compressed, summary = self._importance_based_compress(
            messages, max_chars, estimated_avg_token_per_char
        )

        return compressed, summary

    def _importance_based_compress(
        self,
        messages: List[Dict[str, Any]],
        max_chars: int,
        token_ratio: float,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """Compress based on message importance."""
        # Split into system and conversation messages
        system_msgs = []
        conv_msgs = []

        for msg in messages:
            if msg.get("role") == "system":
                system_msgs.append(msg)
            else:
                conv_msgs.append(msg)

        # Score conversation messages by importance
        scored = []
        for i, msg in enumerate(conv_msgs):
            score = self._score_importance(msg, i, len(conv_msgs))
            scored.append((score, i, msg))

        # Sort by importance (high first), but keep order within same score
        scored.sort(key=lambda x: (-x[0], x[1]))

        # Build compressed conversation
        compressed_conv = []

        # Always keep recent messages
        recent_msgs = conv_msgs[-self._preserve_recent:]
        current_size = 0
        for msg in recent_msgs:
            msg_size = self._content_size([msg])
            if current_size + msg_size <= max_chars * 0.7:
                compressed_conv.append(msg)
                current_size += msg_size

        # Add high-importance messages if space allowing
        for score, idx, msg in scored:
            if msg in compressed_conv:
                continue
            msg_size = self._content_size([msg])
            if current_size + msg_size <= max_chars * 0.9:
                compressed_conv.append(msg)
                current_size += msg_size

        # 修复：使用预构建的 index_map 避免 O(N²) 排序
        # 之前用 conv_msgs.index(m) 在长对话中（5000+ 消息）会产生明显延迟
        index_map = {id(m): i for i, m in enumerate(conv_msgs)}
        compressed_conv.sort(key=lambda m: index_map.get(id(m), 0))

        # Build final result
        result = list(system_msgs) + compressed_conv if self._preserve_system else compressed_conv

        # Calculate compression ratio
        original_chars = sum(len(m.get("content", "")) for m in messages)
        compressed_chars = self._content_size(result)
        ratio = (1 - compressed_chars / original_chars) * 100 if original_chars > 0 else 0

        summary = f"compressed {len(messages)} → {len(result)} messages ({ratio:.0f}% reduction)"

        return result, summary

    def _score_importance(
        self, msg: Dict[str, Any], index: int, total: int
    ) -> float:
        """Score message importance (0.0-1.0)."""
        content = msg.get("content", "")
        score = 0.5  # Baseline

        # Recent messages are more important
        recency_factor = index / total  # 0 = oldest, 1 = newest
        score += recency_factor * 0.3

        # Role importance
        role = msg.get("role", "")
        if role == "user":
            score += 0.1
        elif role == "assistant":
            score += 0.05

        # Check importance patterns
        for pattern_type, patterns in IMPORTANCE_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, content, re.IGNORECASE):
                    if pattern_type in ("decision", "fact", "action"):
                        score += 0.2
                    elif pattern_type == "error":
                        score += 0.15
                    break

        # Penalize redundant messages
        for pattern in REDUNDANCY_PATTERNS:
            if re.search(pattern, content):
                score -= 0.1
                break

        # Penalize very short messages (likely acknowledgments)
        if len(content) < 20:
            score -= 0.1

        # Penalize very long messages (hard to preserve context)
        if len(content) > 2000:
            score -= 0.1

        return max(0.0, min(1.0, score))

    def _content_size(self, messages: List[Dict[str, Any]]) -> int:
        """Calculate total content size of messages."""
        return sum(len(m.get("content", "")) for m in messages)

    def generate_summary_replacement(
        self,
        messages: List[Dict[str, Any]],
        lang: str = "zh",
    ) -> Dict[str, Any]:
        """Generate a summary message to replace a block of old messages.

        Used for more aggressive compression: summarize old messages
        and replace them with a single summary message.
        """
        if lang.startswith("zh"):
            return self._generate_summary_zh(messages)
        else:
            return self._generate_summary_en(messages)

    def _generate_summary_zh(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Generate Chinese summary."""
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]

        summary_parts = ["【之前对话摘要】"]

        if user_msgs:
            topics = self._extract_topics_from_messages(user_msgs)
            if topics:
                summary_parts.append(f"讨论话题：{', '.join(topics)}")

        if len(messages) > 10:
            summary_parts.append(f"共 {len(messages)} 条消息")

        # Extract key points
        key_points = self._extract_key_points(messages)
        if key_points:
            summary_parts.append("关键信息：")
            for point in key_points[:3]:
                summary_parts.append(f"• {point}")

        summary_parts.append("\n如需了解详情，请查阅之前的对话记录。")

        return {
            "role": "system",
            "content": "\n".join(summary_parts),
        }

    def _generate_summary_en(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Generate English summary."""
        user_msgs = [m for m in messages if m.get("role") == "user"]

        summary_parts = ["[Previous Conversation Summary]"]

        if user_msgs:
            topics = self._extract_topics_from_messages(user_msgs)
            if topics:
                summary_parts.append(f"Topics discussed: {', '.join(topics)}")

        if len(messages) > 10:
            summary_parts.append(f"({len(messages)} messages)")

        key_points = self._extract_key_points(messages)
        if key_points:
            summary_parts.append("Key information:")
            for point in key_points[:3]:
                summary_parts.append(f"• {point}")

        summary_parts.append("\nSee previous conversation for details.")

        return {
            "role": "system",
            "content": "\n".join(summary_parts),
        }

    def _extract_topics_from_messages(self, messages: List[Dict[str, Any]]) -> List[str]:
        """Extract topic keywords from messages."""
        all_text = " ".join(m.get("content", "") for m in messages).lower()

        topics = []
        topic_keywords = {
            "编程": ["code", "python", "函数", "class", "编程"],
            "搜索": ["search", "搜索", "查找", "find"],
            "文档": ["document", "文档", "file", "文件"],
            "系统": ["system", "系统", "shell", "命令"],
            "数据": ["data", "数据", "分析", "analysis"],
            "错误": ["error", "bug", "错误", "调试"],
            "学习": ["learn", "学习", "教程", "explain"],
        }

        for topic, keywords in topic_keywords.items():
            if any(kw in all_text for kw in keywords):
                topics.append(topic)

        return topics[:5]

    def _extract_key_points(self, messages: List[Dict[str, Any]]) -> List[str]:
        """Extract key points (decisions, facts, actions) from messages."""
        key_points = []

        for msg in messages:
            content = msg.get("content", "")

            # Look for decision patterns
            for pattern in IMPORTANCE_PATTERNS.get("decision", []):
                matches = re.findall(f"[^.。]*[是为][^.。]*{pattern}[^.。]*", content)
                for m in matches[:1]:
                    if len(m) > 10 and len(m) < 200:
                        key_points.append(m.strip())

            # Look for fact patterns
            for pattern in IMPORTANCE_PATTERNS.get("fact", []):
                matches = re.findall(f"[^.。]*[0-9]+[^.。]*", content)
                for m in matches[:1]:
                    if len(m) > 10 and len(m) < 200:
                        key_points.append(m.strip())

        return list(dict.fromkeys(key_points))[:5]  # Dedupe, keep order


class TieredCompressor:
    """Tiered context compression based on conversation complexity.

    Adapts compression strategy to the tier of the conversation.
    """

    @staticmethod
    def for_tier(tier: str) -> ContextCompressor:
        """Get appropriate compressor for a tier."""
        tier_configs = {
            "trivial": (MAX_CONTEXT_TOKENS_TRIVIAL, 2, True),
            "simple": (MAX_CONTEXT_TOKENS_SIMPLE, 3, True),
            "complex": (MAX_CONTEXT_TOKENS_COMPLEX, 5, True),
            "expert": (MAX_CONTEXT_TOKENS_EXPERT, 8, True),
        }
        max_tokens, preserve_recent, preserve_system = tier_configs.get(
            tier, (MAX_CONTEXT_TOKENS_COMPLEX, 5, True)
        )
        return ContextCompressor(
            max_tokens=max_tokens,
            preserve_recent=preserve_recent,
            preserve_system=preserve_system,
        )