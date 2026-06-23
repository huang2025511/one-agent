"""对话分析与洞察 — 对话统计、用户画像、话题分析和情感分析。

提供：
  - 对话统计分析（消息数、Token用量、活跃时间分布）
  - 用户画像构建（兴趣标签、使用习惯）
  - 话题聚类与趋势分析
  - 情感分析与满意度评估
  - 数据可视化仪表盘
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.plugin import Plugin

logger = logging.getLogger(__name__)


@dataclass
class ConversationStats:
    """对话统计数据类。"""
    total_messages: int = 0
    total_sessions: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    avg_messages_per_session: float = 0.0
    avg_response_time: float = 0.0
    peak_hour: int = 0
    active_days: int = 0
    user_message_count: int = 0
    assistant_message_count: int = 0
    tool_call_count: int = 0


@dataclass
class UserProfile:
    """用户画像类。"""
    user_id: str
    interest_tags: Dict[str, float] = field(default_factory=dict)
    usage_pattern: Dict[str, Any] = field(default_factory=dict)
    preferred_topics: List[str] = field(default_factory=list)
    active_times: List[int] = field(default_factory=list)
    total_sessions: int = 0
    total_messages: int = 0
    first_seen: float = 0
    last_seen: float = 0
    satisfaction_score: float = 0.0


@dataclass
class TopicCluster:
    """话题聚类类。"""
    topic_id: str
    keywords: List[str]
    message_count: int = 0
    sample_messages: List[str] = field(default_factory=list)
    trend: float = 0.0  # 趋势：正数上升，负数下降


@dataclass
class SentimentAnalysis:
    """情感分析结果类。"""
    positive: float = 0.0
    neutral: float = 0.0
    negative: float = 0.0
    satisfaction_score: float = 0.0
    confidence: float = 0.0


class ConversationAnalyzer:
    """对话分析器 — 分析对话数据的统计信息。"""

    def analyze_messages(self, messages: List[Dict[str, Any]]) -> ConversationStats:
        """分析消息列表，返回统计数据。"""
        stats = ConversationStats()

        if not messages:
            return stats

        stats.total_messages = len(messages)
        stats.user_message_count = sum(1 for m in messages if m.get("role") == "user")
        stats.assistant_message_count = sum(1 for m in messages if m.get("role") == "assistant")

        # 统计Token
        for msg in messages:
            stats.total_tokens += msg.get("token_count", 0) or 0
            stats.total_cost += msg.get("cost", 0) or 0
            if "tool_calls" in msg:
                stats.tool_call_count += len(msg["tool_calls"])

        # 计算活跃时间段
        hour_counts = defaultdict(int)
        day_counts = defaultdict(int)
        for msg in messages:
            ts = msg.get("timestamp", time.time())
            dt = datetime.fromtimestamp(ts)
            hour_counts[dt.hour] += 1
            day_counts[dt.date()] += 1

        if hour_counts:
            stats.peak_hour = max(hour_counts, key=hour_counts.get)
        stats.active_days = len(day_counts)

        return stats


class UserProfiler:
    """用户画像构建器 — 基于对话历史构建用户画像。"""

    # 兴趣关键词分类
    INTEREST_KEYWORDS = {
        "编程开发": ["代码", "编程", "函数", "Python", "Java", "JavaScript", "bug", "debug", "算法", "框架"],
        "数据分析": ["数据", "统计", "分析", "图表", "可视化", "Excel", "SQL", "机器学习"],
        "写作创作": ["文章", "写作", "文案", "故事", "小说", "诗歌", "创作", "剧本"],
        "学习教育": ["学习", "考试", "教程", "知识", "解释", "原理", "方法", "技巧"],
        "生活娱乐": ["电影", "音乐", "游戏", "旅游", "美食", "健身", "运动", "娱乐"],
        "商业职场": ["工作", "项目", "管理", "团队", "简历", "面试", "创业", "投资"],
        "健康医疗": ["健康", "疾病", "治疗", "医生", "药品", "健身", "饮食", "心理"],
        "科技前沿": ["AI", "人工智能", "区块链", "元宇宙", "VR", "芯片", "量子"],
    }

    def build_profile(self, user_id: str, messages: List[Dict[str, Any]]) -> UserProfile:
        """基于对话历史构建用户画像。"""
        profile = UserProfile(user_id=user_id)

        if not messages:
            return profile

        profile.total_messages = len(messages)

        # 提取用户消息
        user_messages = [m for m in messages if m.get("role") == "user"]

        # 分析兴趣标签
        tag_scores = defaultdict(float)
        all_text = " ".join([m.get("content", "") for m in user_messages])

        for tag, keywords in self.INTEREST_KEYWORDS.items():
            score = 0
            for keyword in keywords:
                count = all_text.count(keyword)
                score += count * (1 if len(keyword) > 2 else 0.5)
            if score > 0:
                tag_scores[tag] = score

        # 归一化
        if tag_scores:
            max_score = max(tag_scores.values())
            for tag in tag_scores:
                tag_scores[tag] = tag_scores[tag] / max_score

        profile.interest_tags = dict(tag_scores)

        # 偏好话题（Top 3标签）
        sorted_tags = sorted(tag_scores.items(), key=lambda x: x[1], reverse=True)
        profile.preferred_topics = [tag for tag, _ in sorted_tags[:3]]

        # 活跃时间
        hour_counts = defaultdict(int)
        for msg in messages:
            ts = msg.get("timestamp", time.time())
            dt = datetime.fromtimestamp(ts)
            hour_counts[dt.hour] += 1
        profile.active_times = sorted(hour_counts.keys(), key=lambda h: hour_counts[h], reverse=True)[:5]

        # 首次/最后出现时间
        timestamps = [m.get("timestamp", time.time()) for m in messages]
        if timestamps:
            profile.first_seen = min(timestamps)
            profile.last_seen = max(timestamps)

        return profile


class TopicAnalyzer:
    """话题分析器 — 话题聚类和趋势分析。"""

    def extract_keywords(self, text: str, top_n: int = 10) -> List[str]:
        """提取文本关键词（简化版：基于词频）。"""
        # 简单分词（按标点和空格）
        import re
        words = re.findall(r'[\u4e00-\u9fa5]+|[a-zA-Z]+', text)
        # 过滤停用词（简化版）
        stop_words = {"的", "了", "是", "在", "我", "有", "和", "就", "不", "人", "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有", "看", "好", "自己", "这"}
        words = [w for w in words if w not in stop_words and len(w) > 1]

        counter = Counter(words)
        return [word for word, _ in counter.most_common(top_n)]

    def cluster_topics(self, messages: List[Dict[str, Any]], num_topics: int = 5) -> List[TopicCluster]:
        """对消息进行话题聚类（简化版：基于关键词相似度）。"""
        if not messages:
            return []

        # 提取所有用户消息
        user_messages = [m.get("content", "") for m in messages if m.get("role") == "user"]

        if not user_messages:
            return []

        # 简单聚类：按时间窗口分组
        clusters = []
        batch_size = max(1, len(user_messages) // num_topics)

        for i in range(num_topics):
            start = i * batch_size
            end = min(start + batch_size, len(user_messages))
            batch = user_messages[start:end]

            if not batch:
                continue

            batch_text = " ".join(batch)
            keywords = self.extract_keywords(batch_text, top_n=5)

            cluster = TopicCluster(
                topic_id=f"topic_{i}",
                keywords=keywords,
                message_count=len(batch),
                sample_messages=batch[:3],
            )
            clusters.append(cluster)

        return clusters


class SentimentAnalyzer:
    """情感分析器 — 基于关键词的简单情感分析。"""

    POSITIVE_WORDS = [
        "好", "棒", "优秀", "喜欢", "满意", "谢谢", "感谢", "赞", "厉害", "完美",
        "不错", "很好", "非常好", "太好了", "开心", "高兴", "成功", "解决", "有用", "帮助",
    ]

    NEGATIVE_WORDS = [
        "差", "坏", "糟糕", "讨厌", "不满", "生气", "愤怒", "失望", "失败", "错误",
        "不好", "不行", "没用", "问题", "bug", "错误", "慢", "卡", "崩溃", "投诉",
    ]

    def analyze(self, text: str) -> SentimentAnalysis:
        """分析文本情感。"""
        result = SentimentAnalysis()

        if not text:
            result.neutral = 1.0
            return result

        text_lower = text.lower()

        positive_count = sum(1 for word in self.POSITIVE_WORDS if word in text_lower)
        negative_count = sum(1 for word in self.NEGATIVE_WORDS if word in text_lower)

        total = positive_count + negative_count

        if total == 0:
            result.neutral = 1.0
            result.confidence = 0.5
        else:
            result.positive = positive_count / total
            result.negative = negative_count / total
            result.neutral = max(0, 1.0 - abs(positive_count - negative_count) / max(total, 1))
            result.confidence = min(1.0, total / 5.0)  # 关键词越多置信度越高

        # 满意度分数（0-1）
        result.satisfaction_score = result.positive * 0.8 + result.neutral * 0.5

        return result

    def analyze_conversation(self, messages: List[Dict[str, Any]]) -> SentimentAnalysis:
        """分析整个对话的情感倾向。"""
        user_messages = [m.get("content", "") for m in messages if m.get("role") == "user"]

        if not user_messages:
            return SentimentAnalysis(neutral=1.0)

        total_positive = 0
        total_negative = 0
        total_neutral = 0
        total_confidence = 0

        for msg in user_messages:
            sentiment = self.analyze(msg)
            total_positive += sentiment.positive
            total_negative += sentiment.negative
            total_neutral += sentiment.neutral
            total_confidence += sentiment.confidence

        n = len(user_messages)
        result = SentimentAnalysis(
            positive=total_positive / n,
            negative=total_negative / n,
            neutral=total_neutral / n,
            confidence=total_confidence / n,
        )
        result.satisfaction_score = result.positive * 0.8 + result.neutral * 0.5

        return result


class AnalyticsPlugin(Plugin):
    """对话分析与洞察插件。"""

    name = "analytics"

    def __init__(self):
        super().__init__()
        self._conversation_analyzer = ConversationAnalyzer()
        self._user_profiler = UserProfiler()
        self._topic_analyzer = TopicAnalyzer()
        self._sentiment_analyzer = SentimentAnalyzer()
        self._stats_cache: Dict[str, ConversationStats] = {}
        self._profile_cache: Dict[str, UserProfile] = {}

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        logger.info("Analytics plugin configured")

    def analyze_conversation(self, messages: List[Dict[str, Any]]) -> ConversationStats:
        """分析对话统计。"""
        return self._conversation_analyzer.analyze_messages(messages)

    def build_user_profile(self, user_id: str, messages: List[Dict[str, Any]]) -> UserProfile:
        """构建用户画像。"""
        profile = self._user_profiler.build_profile(user_id, messages)
        self._profile_cache[user_id] = profile
        return profile

    def analyze_topics(self, messages: List[Dict[str, Any]], num_topics: int = 5) -> List[TopicCluster]:
        """话题聚类分析。"""
        return self._topic_analyzer.cluster_topics(messages, num_topics)

    def analyze_sentiment(self, text: str) -> SentimentAnalysis:
        """单文本情感分析。"""
        return self._sentiment_analyzer.analyze(text)

    def analyze_conversation_sentiment(self, messages: List[Dict[str, Any]]) -> SentimentAnalysis:
        """对话情感分析。"""
        return self._sentiment_analyzer.analyze_conversation(messages)

    def get_full_report(self, user_id: str, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """获取完整分析报告。"""
        stats = self.analyze_conversation(messages)
        profile = self.build_user_profile(user_id, messages)
        topics = self.analyze_topics(messages)
        sentiment = self.analyze_conversation_sentiment(messages)

        return {
            "statistics": {
                "total_messages": stats.total_messages,
                "total_sessions": stats.total_sessions,
                "total_tokens": stats.total_tokens,
                "total_cost": stats.total_cost,
                "avg_messages_per_session": stats.avg_messages_per_session,
                "peak_hour": stats.peak_hour,
                "active_days": stats.active_days,
                "tool_call_count": stats.tool_call_count,
            },
            "user_profile": {
                "interest_tags": profile.interest_tags,
                "preferred_topics": profile.preferred_topics,
                "active_times": profile.active_times,
                "total_sessions": profile.total_sessions,
                "first_seen": profile.first_seen,
                "last_seen": profile.last_seen,
            },
            "topics": [
                {
                    "topic_id": t.topic_id,
                    "keywords": t.keywords,
                    "message_count": t.message_count,
                }
                for t in topics
            ],
            "sentiment": {
                "positive": sentiment.positive,
                "neutral": sentiment.neutral,
                "negative": sentiment.negative,
                "satisfaction_score": sentiment.satisfaction_score,
                "confidence": sentiment.confidence,
            },
        }

    def get_conversation_analyzer(self) -> ConversationAnalyzer:
        """获取对话分析器。"""
        return self._conversation_analyzer

    def get_user_profiler(self) -> UserProfiler:
        """获取用户画像构建器。"""
        return self._user_profiler

    def get_topic_analyzer(self) -> TopicAnalyzer:
        """获取话题分析器。"""
        return self._topic_analyzer

    def get_sentiment_analyzer(self) -> SentimentAnalyzer:
        """获取情感分析器。"""
        return self._sentiment_analyzer