"""推荐引擎 — 协同过滤、内容推荐、上下文感知推荐与A/B测试。

提供：
  - 协同过滤（基于用户/基于物品）
  - 内容推荐（TF-IDF相似度）
  - 上下文感知推荐（时间/位置/设备）
  - 冷启动处理（新用户/新物品）
  - 实时推荐流
  - A/B测试框架与效果评估
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from core.plugin import Plugin

logger = logging.getLogger(__name__)


@dataclass
class Rating:
    """用户评分记录。"""
    user_id: str
    item_id: str
    score: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class Item:
    """物品信息。"""
    item_id: str
    title: str = ""
    category: str = ""
    tags: List[str] = field(default_factory=list)
    description: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Recommendation:
    """推荐结果。"""
    item_id: str
    score: float
    reason: str = ""


@dataclass
class UserContext:
    """用户上下文信息。"""
    user_id: str
    timestamp: float = field(default_factory=time.time)
    location: str = ""
    device: str = ""
    session_items: List[str] = field(default_factory=list)


class CollaborativeFilter:
    """协同过滤推荐器 — 基于用户和物品的协同过滤。"""

    def __init__(self):
        self._ratings: List[Rating] = []
        self._user_ratings: Dict[str, Dict[str, float]] = {}  # user -> {item: score}
        self._item_ratings: Dict[str, Dict[str, float]] = {}  # item -> {user: score}
        self._user_similarity_cache: Dict[Tuple[str, str], float] = {}
        self._item_similarity_cache: Dict[Tuple[str, str], float] = {}

    def add_rating(self, rating: Rating):
        """添加评分记录。"""
        self._ratings.append(rating)
        if rating.user_id not in self._user_ratings:
            self._user_ratings[rating.user_id] = {}
        self._user_ratings[rating.user_id][rating.item_id] = rating.score

        if rating.item_id not in self._item_ratings:
            self._item_ratings[rating.item_id] = {}
        self._item_ratings[rating.item_id][rating.user_id] = rating.score

        # 清除相似度缓存
        self._user_similarity_cache.clear()
        self._item_similarity_cache.clear()

    def _cosine_similarity(self, vec_a: Dict[str, float], vec_b: Dict[str, float]) -> float:
        """计算余弦相似度。"""
        common_keys = set(vec_a.keys()) & set(vec_b.keys())
        if not common_keys:
            return 0.0

        dot_product = sum(vec_a[k] * vec_b[k] for k in common_keys)
        norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
        norm_b = math.sqrt(sum(v * v for v in vec_b.values()))

        if norm_a == 0 or norm_b == 0:
            return 0.0

        return dot_product / (norm_a * norm_b)

    def _pearson_similarity(self, vec_a: Dict[str, float], vec_b: Dict[str, float]) -> float:
        """计算皮尔逊相关系数。"""
        common_keys = set(vec_a.keys()) & set(vec_b.keys())
        if len(common_keys) < 2:
            return 0.0

        values_a = [vec_a[k] for k in common_keys]
        values_b = [vec_b[k] for k in common_keys]

        mean_a = sum(values_a) / len(values_a)
        mean_b = sum(values_b) / len(values_b)

        numerator = sum((vec_a[k] - mean_a) * (vec_b[k] - mean_b) for k in common_keys)
        denom_a = math.sqrt(sum((vec_a[k] - mean_a) ** 2 for k in common_keys))
        denom_b = math.sqrt(sum((vec_b[k] - mean_b) ** 2 for k in common_keys))

        if denom_a == 0 or denom_b == 0:
            return 0.0

        return numerator / (denom_a * denom_b)

    def user_based_recommend(self, user_id: str, top_k: int = 10, n_neighbors: int = 20) -> List[Recommendation]:
        """基于用户的协同过滤推荐。"""
        if user_id not in self._user_ratings:
            return []

        target_ratings = self._user_ratings[user_id]
        rated_items = set(target_ratings.keys())

        # 计算与所有其他用户的相似度
        similarities = []
        for other_user, other_ratings in self._user_ratings.items():
            if other_user == user_id:
                continue
            sim = self._cosine_similarity(target_ratings, other_ratings)
            if sim > 0:
                similarities.append((other_user, sim))

        # 取最相似的n个用户
        similarities.sort(key=lambda x: x[1], reverse=True)
        neighbors = similarities[:n_neighbors]

        if not neighbors:
            return []

        # 加权预测评分
        scores: Dict[str, float] = defaultdict(float)
        sim_sums: Dict[str, float] = defaultdict(float)

        for neighbor_id, sim in neighbors:
            neighbor_ratings = self._user_ratings[neighbor_id]
            for item_id, rating in neighbor_ratings.items():
                if item_id not in rated_items:
                    scores[item_id] += sim * rating
                    sim_sums[item_id] += sim

        # 归一化
        recommendations = []
        for item_id, score in scores.items():
            if sim_sums[item_id] > 0:
                final_score = score / sim_sums[item_id]
                recommendations.append(Recommendation(
                    item_id=item_id,
                    score=final_score,
                    reason=f"基于{len(neighbors)}位相似用户推荐"
                ))

        recommendations.sort(key=lambda x: x.score, reverse=True)
        return recommendations[:top_k]

    def item_based_recommend(self, user_id: str, top_k: int = 10) -> List[Recommendation]:
        """基于物品的协同过滤推荐。"""
        if user_id not in self._user_ratings:
            return []

        user_ratings = self._user_ratings[user_id]
        rated_items = set(user_ratings.keys())

        # 计算未评分物品与已评分物品的相似度
        scores: Dict[str, float] = defaultdict(float)
        sim_sums: Dict[str, float] = defaultdict(float)

        all_items = set(self._item_ratings.keys())
        candidate_items = all_items - rated_items

        for candidate_item in candidate_items:
            candidate_ratings = self._item_ratings[candidate_item]

            for rated_item, user_score in user_ratings.items():
                rated_item_ratings = self._item_ratings.get(rated_item, {})
                sim = self._cosine_similarity(candidate_ratings, rated_item_ratings)

                if sim > 0:
                    scores[candidate_item] += sim * user_score
                    sim_sums[candidate_item] += sim

        recommendations = []
        for item_id, score in scores.items():
            if sim_sums[item_id] > 0:
                final_score = score / sim_sums[item_id]
                recommendations.append(Recommendation(
                    item_id=item_id,
                    score=final_score,
                    reason="基于物品相似度推荐"
                ))

        recommendations.sort(key=lambda x: x.score, reverse=True)
        return recommendations[:top_k]

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息。"""
        return {
            "total_ratings": len(self._ratings),
            "total_users": len(self._user_ratings),
            "total_items": len(self._item_ratings),
        }


class ContentRecommender:
    """内容推荐器 — 基于内容特征的推荐。"""

    def __init__(self):
        self._items: Dict[str, Item] = {}
        self._tfidf_cache: Dict[str, Dict[str, float]] = {}
        self._idf: Dict[str, float] = {}
        self._vocabulary: Set[str] = set()

    def add_item(self, item: Item):
        """添加物品。"""
        self._items[item.item_id] = item
        self._rebuild_index()

    def _tokenize(self, text: str) -> List[str]:
        """简单分词。"""
        import re
        words = re.findall(r'[\u4e00-\u9fa5]+|[a-zA-Z]+', text.lower())
        return [w for w in words if len(w) > 1]

    def _rebuild_index(self):
        """重建TF-IDF索引。"""
        # 构建文档集合
        documents = {}
        for item_id, item in self._items.items():
            text = f"{item.title} {item.description} {' '.join(item.tags)}"
            tokens = self._tokenize(text)
            documents[item_id] = tokens
            self._vocabulary.update(tokens)

        # 计算IDF
        total_docs = len(documents)
        doc_freq = defaultdict(int)
        for tokens in documents.values():
            for token in set(tokens):
                doc_freq[token] += 1

        self._idf = {
            token: math.log((total_docs + 1) / (freq + 1)) + 1
            for token, freq in doc_freq.items()
        }

        # 计算TF-IDF
        self._tfidf_cache = {}
        for item_id, tokens in documents.items():
            tf = defaultdict(int)
            for token in tokens:
                tf[token] += 1

            total_tokens = len(tokens)
            self._tfidf_cache[item_id] = {
                token: (count / total_tokens) * self._idf.get(token, 0)
                for token, count in tf.items()
            }

    def recommend(self, user_liked_items: List[str], top_k: int = 10) -> List[Recommendation]:
        """基于用户喜欢的物品推荐相似物品。"""
        if not user_liked_items or not self._items:
            return []

        # 构建用户偏好向量
        user_profile: Dict[str, float] = defaultdict(float)
        valid_items = 0
        for item_id in user_liked_items:
            if item_id in self._tfidf_cache:
                for token, weight in self._tfidf_cache[item_id].items():
                    user_profile[token] += weight
                valid_items += 1

        if valid_items == 0:
            return []

        # 归一化
        for token in user_profile:
            user_profile[token] /= valid_items

        # 计算与所有未喜欢物品的相似度
        liked_set = set(user_liked_items)
        recommendations = []

        for item_id, item_tfidf in self._tfidf_cache.items():
            if item_id in liked_set:
                continue

            # 余弦相似度
            dot_product = sum(user_profile.get(t, 0) * w for t, w in item_tfidf.items())
            norm_user = math.sqrt(sum(v * v for v in user_profile.values()))
            norm_item = math.sqrt(sum(v * v for v in item_tfidf.values()))

            if norm_user > 0 and norm_item > 0:
                similarity = dot_product / (norm_user * norm_item)
                if similarity > 0:
                    recommendations.append(Recommendation(
                        item_id=item_id,
                        score=similarity,
                        reason="基于内容相似度推荐"
                    ))

        recommendations.sort(key=lambda x: x.score, reverse=True)
        return recommendations[:top_k]

    def find_similar_items(self, item_id: str, top_k: int = 5) -> List[Recommendation]:
        """查找与指定物品最相似的其他物品。"""
        if item_id not in self._tfidf_cache:
            return []

        target_tfidf = self._tfidf_cache[item_id]
        recommendations = []

        for other_id, other_tfidf in self._tfidf_cache.items():
            if other_id == item_id:
                continue

            similarity = self._cosine_sim(target_tfidf, other_tfidf)
            if similarity > 0:
                recommendations.append(Recommendation(
                    item_id=other_id,
                    score=similarity,
                    reason=f"与物品 {item_id} 相似"
                ))

        recommendations.sort(key=lambda x: x.score, reverse=True)
        return recommendations[:top_k]

    def _cosine_sim(self, vec_a: Dict[str, float], vec_b: Dict[str, float]) -> float:
        """计算余弦相似度。"""
        common_keys = set(vec_a.keys()) & set(vec_b.keys())
        if not common_keys:
            return 0.0
        dot = sum(vec_a[k] * vec_b[k] for k in common_keys)
        norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
        norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


class ContextAwareRecommender:
    """上下文感知推荐器 — 考虑时间/位置/设备等上下文。"""

    # 时间段权重
    TIME_WEIGHTS = {
        "morning": {"news": 1.5, "education": 1.3, "business": 1.2},
        "afternoon": {"business": 1.3, "technology": 1.2, "education": 1.1},
        "evening": {"entertainment": 1.5, "music": 1.3, "social": 1.2},
        "night": {"entertainment": 1.4, "reading": 1.3, "music": 1.2},
    }

    # 设备权重
    DEVICE_WEIGHTS = {
        "mobile": {"social": 1.3, "entertainment": 1.2, "news": 1.1},
        "desktop": {"business": 1.3, "technology": 1.2, "education": 1.1},
        "tablet": {"education": 1.3, "reading": 1.2, "entertainment": 1.1},
    }

    def __init__(self):
        self._items: Dict[str, Item] = {}
        self._base_recommender: Optional[CollaborativeFilter] = None

    def set_base_recommender(self, recommender: CollaborativeFilter):
        """设置基础推荐器。"""
        self._base_recommender = recommender

    def add_item(self, item: Item):
        """添加物品。"""
        self._items[item.item_id] = item

    def _get_time_period(self, timestamp: float) -> str:
        """获取时间段。"""
        from datetime import datetime
        hour = datetime.fromtimestamp(timestamp).hour
        if 6 <= hour < 12:
            return "morning"
        elif 12 <= hour < 18:
            return "afternoon"
        elif 18 <= hour < 22:
            return "evening"
        else:
            return "night"

    def recommend(self, context: UserContext, top_k: int = 10) -> List[Recommendation]:
        """基于上下文的推荐。"""
        # 获取基础推荐
        if self._base_recommender:
            base_recs = self._base_recommender.user_based_recommend(context.user_id, top_k=top_k * 2)
        else:
            base_recs = [
                Recommendation(item_id=item_id, score=1.0, reason="热门推荐")
                for item_id in list(self._items.keys())[:top_k * 2]
            ]

        if not base_recs:
            return []

        # 计算上下文权重
        time_period = self._get_time_period(context.timestamp)
        time_weights = self.TIME_WEIGHTS.get(time_period, {})
        device_weights = self.DEVICE_WEIGHTS.get(context.device, {})

        # 调整推荐分数
        adjusted_recs = []
        for rec in base_recs:
            item = self._items.get(rec.item_id)
            if not item:
                adjusted_recs.append(rec)
                continue

            category = item.category.lower()
            time_boost = time_weights.get(category, 1.0)
            device_boost = device_weights.get(category, 1.0)

            adjusted_score = rec.score * time_boost * device_boost
            reasons = [rec.reason]
            if time_boost > 1.0:
                reasons.append(f"适合{time_period}时段")
            if device_boost > 1.0:
                reasons.append(f"适合{context.device}设备")

            adjusted_recs.append(Recommendation(
                item_id=rec.item_id,
                score=adjusted_score,
                reason="；".join(reasons)
            ))

        adjusted_recs.sort(key=lambda x: x.score, reverse=True)
        return adjusted_recs[:top_k]


class ColdStartHandler:
    """冷启动处理器 — 解决新用户和新物品的推荐问题。"""

    def __init__(self):
        self._items: Dict[str, Item] = {}
        self._item_popularity: Dict[str, float] = defaultdict(float)
        self._item_ratings_count: Dict[str, int] = defaultdict(int)

    def add_item(self, item: Item):
        """添加物品。"""
        self._items[item.item_id] = item

    def record_interaction(self, item_id: str, score: float):
        """记录用户与物品的交互。"""
        self._item_ratings_count[item_id] += 1
        # 滑动平均更新流行度
        old_pop = self._item_popularity.get(item_id, 0)
        count = self._item_ratings_count[item_id]
        self._item_popularity[item_id] = (old_pop * (count - 1) + score) / count

    def recommend_for_new_user(self, user_info: Dict[str, Any] = None, top_k: int = 10) -> List[Recommendation]:
        """为新用户推荐（热门 + 随机 + 基于注册信息）。"""
        recommendations = []

        # 策略1：热门推荐（占60%）
        popular_items = sorted(
            self._item_popularity.items(),
            key=lambda x: x[1],
            reverse=True
        )[:int(top_k * 0.6)]

        for item_id, score in popular_items:
            recommendations.append(Recommendation(
                item_id=item_id,
                score=score,
                reason="热门推荐"
            ))

        # 策略2：基于注册信息推荐（如果有）
        if user_info and "interests" in user_info:
            interests = user_info["interests"]
            matched = [
                (item_id, item) for item_id, item in self._items.items()
                if any(interest.lower() in item.category.lower() or
                       interest.lower() in " ".join(item.tags).lower()
                       for interest in interests)
            ]
            for item_id, item in matched[:int(top_k * 0.2)]:
                recommendations.append(Recommendation(
                    item_id=item_id,
                    score=0.8,
                    reason=f"基于兴趣「{', '.join(interests)}」推荐"
                ))

        # 策略3：随机推荐（补充剩余）
        remaining = top_k - len(recommendations)
        if remaining > 0:
            all_items = list(self._items.keys())
            recommended_ids = {r.item_id for r in recommendations}
            candidates = [iid for iid in all_items if iid not in recommended_ids]
            random.shuffle(candidates)
            for item_id in candidates[:remaining]:
                recommendations.append(Recommendation(
                    item_id=item_id,
                    score=0.5,
                    reason="探索性推荐"
                ))

        return recommendations[:top_k]

    def recommend_new_item(self, item_id: str, top_k: int = 10) -> List[str]:
        """为新物品找到可能感兴趣的用户（基于物品特征匹配）。"""
        # 简化版：返回所有用户（实际应基于物品特征匹配用户兴趣）
        return []

    def get_popular_items(self, top_k: int = 10) -> List[Recommendation]:
        """获取热门物品。"""
        popular = sorted(
            self._item_popularity.items(),
            key=lambda x: x[1],
            reverse=True
        )[:top_k]
        return [
            Recommendation(item_id=item_id, score=score, reason="热门物品")
            for item_id, score in popular
        ]


class RealtimeRecommender:
    """实时推荐流 — 基于用户最近行为的实时推荐。"""

    def __init__(self, window_size: int = 10):
        self._window_size = window_size
        self._user_sessions: Dict[str, List[str]] = {}  # user -> recent items
        self._item_co_occurrence: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._items: Dict[str, Item] = {}

    def add_item(self, item: Item):
        """添加物品。"""
        self._items[item.item_id] = item

    def record_view(self, user_id: str, item_id: str):
        """记录用户浏览行为。"""
        if user_id not in self._user_sessions:
            self._user_sessions[user_id] = []

        session = self._user_sessions[user_id]

        # 更新共现矩阵
        for recent_item in session:
            self._item_co_occurrence[recent_item][item_id] += 1
            self._item_co_occurrence[item_id][recent_item] += 1

        # 添加到会话（滑动窗口）
        session.append(item_id)
        if len(session) > self._window_size:
            session.pop(0)

    def recommend(self, user_id: str, top_k: int = 10) -> List[Recommendation]:
        """实时推荐。"""
        session = self._user_sessions.get(user_id, [])
        if not session:
            return []

        # 基于最近浏览物品的共现推荐
        scores: Dict[str, float] = defaultdict(float)
        recent_weight = 1.0

        for item_id in reversed(session[-5:]):  # 取最近5个
            co_items = self._item_co_occurrence.get(item_id, {})
            for co_item, count in co_items.items():
                if co_item not in session:  # 排除已浏览
                    scores[co_item] += count * recent_weight
            recent_weight *= 0.8  # 时间衰减

        recommendations = [
            Recommendation(
                item_id=item_id,
                score=score,
                reason="基于最近浏览推荐"
            )
            for item_id, score in scores.items()
        ]

        recommendations.sort(key=lambda x: x.score, reverse=True)
        return recommendations[:top_k]


class RecommendationABTest:
    """A/B测试框架 — 推荐策略对比实验。"""

    def __init__(self):
        self._experiments: Dict[str, Dict[str, Any]] = {}

    def create_experiment(self, experiment_id: str, strategy_a_name: str,
                          strategy_b_name: str, description: str = "") -> bool:
        """创建A/B测试实验。"""
        if experiment_id in self._experiments:
            return False

        self._experiments[experiment_id] = {
            "strategy_a": strategy_a_name,
            "strategy_b": strategy_b_name,
            "description": description,
            "impressions_a": 0,
            "clicks_a": 0,
            "conversions_a": 0,
            "impressions_b": 0,
            "clicks_b": 0,
            "conversions_b": 0,
            "items_a": set(),
            "items_b": set(),
            "status": "running",
            "created_at": time.time(),
        }
        return True

    def record_impression(self, experiment_id: str, variant: str, items: List[str]):
        """记录推荐展示。"""
        if experiment_id not in self._experiments:
            return
        exp = self._experiments[experiment_id]
        key = f"impressions_{variant}"
        exp[key] += 1
        exp[f"items_{variant}"].update(items)

    def record_click(self, experiment_id: str, variant: str):
        """记录用户点击。"""
        if experiment_id not in self._experiments:
            return
        exp = self._experiments[experiment_id]
        exp[f"clicks_{variant}"] += 1

    def record_conversion(self, experiment_id: str, variant: str):
        """记录转化。"""
        if experiment_id not in self._experiments:
            return
        exp = self._experiments[experiment_id]
        exp[f"conversions_{variant}"] += 1

    def get_results(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """获取实验结果。"""
        exp = self._experiments.get(experiment_id)
        if not exp:
            return None

        ctr_a = exp["clicks_a"] / exp["impressions_a"] if exp["impressions_a"] > 0 else 0
        ctr_b = exp["clicks_b"] / exp["impressions_b"] if exp["impressions_b"] > 0 else 0
        cvr_a = exp["conversions_a"] / exp["impressions_a"] if exp["impressions_a"] > 0 else 0
        cvr_b = exp["conversions_b"] / exp["impressions_b"] if exp["impressions_b"] > 0 else 0

        # 覆盖率
        total_items = len(self._items) if hasattr(self, '_items') else 100
        coverage_a = len(exp["items_a"]) / total_items if total_items > 0 else 0
        coverage_b = len(exp["items_b"]) / total_items if total_items > 0 else 0

        # 多样性（简化版：基于推荐物品集合的类别数）
        diversity_a = len(exp["items_a"]) / max(exp["impressions_a"], 1)
        diversity_b = len(exp["items_b"]) / max(exp["impressions_b"], 1)

        # 判断胜者
        winner = ""
        if ctr_a > ctr_b * 1.05:
            winner = exp["strategy_a"]
        elif ctr_b > ctr_a * 1.05:
            winner = exp["strategy_b"]
        else:
            winner = "tie"

        return {
            "experiment_id": experiment_id,
            "strategy_a": exp["strategy_a"],
            "strategy_b": exp["strategy_b"],
            "metrics": {
                "A": {
                    "impressions": exp["impressions_a"],
                    "clicks": exp["clicks_a"],
                    "conversions": exp["conversions_a"],
                    "ctr": round(ctr_a, 4),
                    "cvr": round(cvr_a, 4),
                    "coverage": round(coverage_a, 4),
                    "diversity": round(diversity_a, 4),
                },
                "B": {
                    "impressions": exp["impressions_b"],
                    "clicks": exp["clicks_b"],
                    "conversions": exp["conversions_b"],
                    "ctr": round(ctr_b, 4),
                    "cvr": round(cvr_b, 4),
                    "coverage": round(coverage_b, 4),
                    "diversity": round(diversity_b, 4),
                },
            },
            "winner": winner,
            "status": exp["status"],
        }

    def stop_experiment(self, experiment_id: str) -> bool:
        """停止实验。"""
        if experiment_id in self._experiments:
            self._experiments[experiment_id]["status"] = "completed"
            return True
        return False

    def list_experiments(self) -> List[Dict[str, Any]]:
        """列出所有实验。"""
        return [
            {"experiment_id": eid, **{k: v for k, v in exp.items() if k != "items_a" and k != "items_b"}}
            for eid, exp in self._experiments.items()
        ]


class RecommenderPlugin(Plugin):
    """推荐引擎插件。"""

    name = "recommender"

    def __init__(self):
        super().__init__()
        self._cf = CollaborativeFilter()
        self._content_rec = ContentRecommender()
        self._context_rec = ContextAwareRecommender()
        self._cold_start = ColdStartHandler()
        self._realtime = RealtimeRecommender()
        self._ab_test = RecommendationABTest()
        self._items: Dict[str, Item] = {}

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        self._context_rec.set_base_recommender(self._cf)
        logger.info("Recommender plugin configured")

    def add_item(self, item: Item):
        """添加物品到所有推荐器。"""
        self._items[item.item_id] = item
        self._content_rec.add_item(item)
        self._context_rec.add_item(item)
        self._cold_start.add_item(item)
        self._realtime.add_item(item)

    def add_rating(self, user_id: str, item_id: str, score: float):
        """添加评分。"""
        rating = Rating(user_id=user_id, item_id=item_id, score=score)
        self._cf.add_rating(rating)
        self._cold_start.record_interaction(item_id, score)

    def record_view(self, user_id: str, item_id: str):
        """记录浏览行为。"""
        self._realtime.record_view(user_id, item_id)

    def recommend(self, user_id: str, method: str = "cf_user", top_k: int = 10,
                  context: UserContext = None) -> List[Recommendation]:
        """统一推荐接口。

        Args:
            method: cf_user / cf_item / content / context / cold_start / realtime
        """
        if method == "cf_user":
            return self._cf.user_based_recommend(user_id, top_k)
        elif method == "cf_item":
            return self._cf.item_based_recommend(user_id, top_k)
        elif method == "content":
            liked = list(self._cf._user_ratings.get(user_id, {}).keys())
            return self._content_rec.recommend(liked, top_k)
        elif method == "context":
            if context is None:
                context = UserContext(user_id=user_id)
            return self._context_rec.recommend(context, top_k)
        elif method == "cold_start":
            return self._cold_start.recommend_for_new_user(top_k=top_k)
        elif method == "realtime":
            return self._realtime.recommend(user_id, top_k)
        else:
            return []

    def find_similar_items(self, item_id: str, top_k: int = 5) -> List[Recommendation]:
        """查找相似物品。"""
        return self._content_rec.find_similar_items(item_id, top_k)

    def create_ab_test(self, experiment_id: str, strategy_a: str, strategy_b: str,
                       description: str = "") -> bool:
        """创建A/B测试。"""
        return self._ab_test.create_experiment(experiment_id, strategy_a, strategy_b, description)

    def get_ab_test_results(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """获取A/B测试结果。"""
        return self._ab_test.get_results(experiment_id)

    def get_stats(self) -> Dict[str, Any]:
        """获取推荐系统统计。"""
        return {
            "collaborative_filter": self._cf.get_stats(),
            "total_items": len(self._items),
            "ab_tests": len(self._ab_test._experiments),
        }

    def get_cf(self) -> CollaborativeFilter:
        """获取协同过滤推荐器。"""
        return self._cf

    def get_content_recommender(self) -> ContentRecommender:
        """获取内容推荐器。"""
        return self._content_rec

    def get_context_recommender(self) -> ContextAwareRecommender:
        """获取上下文推荐器。"""
        return self._context_rec

    def get_cold_start_handler(self) -> ColdStartHandler:
        """获取冷启动处理器。"""
        return self._cold_start

    def get_realtime_recommender(self) -> RealtimeRecommender:
        """获取实时推荐器。"""
        return self._realtime

    def get_ab_test_framework(self) -> RecommendationABTest:
        """获取A/B测试框架。"""
        return self._ab_test