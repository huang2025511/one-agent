"""主动学习与自我进化模块。

提供：
  - 持续学习器（ContinuousLearner）：从对话中学习新知识，知识更新与维护，学习进度跟踪
  - 不确定性采样（UncertaintySampler）：评估模型对问题的不确定性，选择最有价值的问题进行学习
  - 知识冲突检测（ConflictDetector）：检测新知识与已有知识的冲突，冲突标记与解决策略
  - 技能总结器（SkillSummarizer）：自动总结对话中的技能和经验，技能沉淀与索引
  - 记忆管理器（MemoryManager）：基于遗忘曲线的记忆衰减，记忆强度评估，记忆巩固
  - 自我评估器（SelfEvaluator）：评估自身能力边界，识别知识盲区，生成学习计划
  - ActiveLearningPlugin：整合以上功能的插件类
"""

from __future__ import annotations

import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.plugin import Plugin

logger = logging.getLogger(__name__)

__all__ = [
    "KnowledgeItem",
    "LearningProgress",
    "ContinuousLearner",
    "UncertaintyScore",
    "QuestionCandidate",
    "UncertaintySampler",
    "Conflict",
    "ConflictResolution",
    "ConflictDetector",
    "SkillSummary",
    "SkillIndex",
    "SkillSummarizer",
    "MemoryEntry",
    "MemoryStrength",
    "MemoryManager",
    "CapabilityBoundary",
    "KnowledgeGap",
    "LearningPlan",
    "SelfEvaluator",
    "ActiveLearningPlugin",
]


# ============================================================
# 1. 持续学习器（ContinuousLearner）
# ============================================================

@dataclass
class KnowledgeItem:
    """单条知识条目。"""

    id: str
    content: str
    source: str = "conversation"
    confidence: float = 0.5
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    access_count: int = 0
    last_accessed: Optional[float] = None

    def touch(self) -> None:
        """记录一次访问。"""
        self.access_count += 1
        self.last_accessed = time.time()
        self.updated_at = self.last_accessed


@dataclass
class LearningProgress:
    """学习进度跟踪。"""

    total_learned: int = 0
    total_reviewed: int = 0
    mastery_level: float = 0.0  # 0..1 掌握程度
    last_learning_time: float = field(default_factory=time.time)
    learning_streak: int = 0  # 连续学习天数
    domain_coverage: Dict[str, float] = field(default_factory=dict)


class ContinuousLearner:
    """持续学习器 — 从对话中学习新知识并维护知识库。

    职责：
      - 从对话中提取并学习新知识
      - 知识更新与维护（去重、合并、置信度调整）
      - 学习进度跟踪
    """

    def __init__(self, max_items: int = 5000) -> None:
        self._knowledge: Dict[str, KnowledgeItem] = {}
        self._max_items = max_items
        self._progress = LearningProgress()
        # 内容指纹索引，用于快速去重
        self._fingerprints: Dict[str, str] = {}

    @staticmethod
    def _fingerprint(content: str) -> str:
        """计算内容的简化指纹（用于去重）。"""
        # 归一化：去空白、转小写、截断
        normalized = re.sub(r"\s+", "", content).lower()[:500]
        return normalized

    def learn(self, content: str, source: str = "conversation",
              confidence: float = 0.5, tags: Optional[List[str]] = None) -> KnowledgeItem:
        """学习一条新知识。

        若内容已存在（指纹相同），则更新已有条目的置信度与访问计数；
        否则创建新条目。返回最终的知识条目。
        """
        if not content or not content.strip():
            raise ValueError("学习内容不能为空")

        content = content.strip()
        tags = tags or []
        fp = self._fingerprint(content)

        if fp in self._fingerprints:
            # 已存在：合并更新
            existing_id = self._fingerprints[fp]
            item = self._knowledge[existing_id]
            # 置信度按加权平均提升
            item.confidence = min(1.0, (item.confidence + confidence) / 2 + 0.05)
            for tag in tags:
                if tag not in item.tags:
                    item.tags.append(tag)
            item.touch()
            logger.debug("更新已有知识 %s（置信度=%.2f）", existing_id, item.confidence)
            return item

        # 新知识
        item_id = uuid.uuid4().hex[:12]
        item = KnowledgeItem(
            id=item_id,
            content=content,
            source=source,
            confidence=confidence,
            tags=tags,
        )
        self._knowledge[item_id] = item
        self._fingerprints[fp] = item_id

        # 维护容量上限：淘汰最旧且置信度最低的条目
        if len(self._knowledge) > self._max_items:
            self._evict()

        # 更新进度
        self._progress.total_learned += 1
        self._progress.last_learning_time = time.time()
        self._update_mastery()
        logger.info("学习新知识 %s（来源=%s，置信度=%.2f）", item_id, source, confidence)
        return item

    def _evict(self) -> None:
        """淘汰策略：优先淘汰置信度低且长期未访问的条目。"""
        if not self._knowledge:
            return
        # 综合评分：置信度 * 时间衰减；分数最低者淘汰
        now = time.time()

        def _score(item: KnowledgeItem) -> float:
            age = max(now - (item.last_accessed or item.created_at), 0.0)
            time_factor = math.exp(-age / (30 * 86400))  # 30天半衰
            return item.confidence * time_factor

        worst_id = min(self._knowledge, key=lambda k: _score(self._knowledge[k]))
        worst = self._knowledge.pop(worst_id)
        # 同步清理指纹索引
        fp = self._fingerprint(worst.content)
        if self._fingerprints.get(fp) == worst_id:
            del self._fingerprints[fp]
        logger.debug("淘汰低价值知识 %s", worst_id)

    def update_knowledge(self, item_id: str, content: Optional[str] = None,
                         confidence: Optional[float] = None,
                         tags: Optional[List[str]] = None) -> Optional[KnowledgeItem]:
        """更新指定知识条目。"""
        item = self._knowledge.get(item_id)
        if item is None:
            return None
        if content is not None and content.strip():
            # 内容变更需更新指纹
            old_fp = self._fingerprint(item.content)
            if self._fingerprints.get(old_fp) == item_id:
                del self._fingerprints[old_fp]
            item.content = content.strip()
            new_fp = self._fingerprint(item.content)
            self._fingerprints[new_fp] = item_id
        if confidence is not None:
            item.confidence = max(0.0, min(1.0, confidence))
        if tags is not None:
            item.tags = list(tags)
        item.updated_at = time.time()
        self._update_mastery()
        return item

    def get(self, item_id: str) -> Optional[KnowledgeItem]:
        """按 id 获取知识条目（同时记录访问）。"""
        item = self._knowledge.get(item_id)
        if item is not None:
            item.touch()
        return item

    def search(self, query: str, limit: int = 5) -> List[KnowledgeItem]:
        """关键词搜索知识库。"""
        query_lower = query.lower()
        scored: List[Tuple[float, KnowledgeItem]] = []
        for item in self._knowledge.values():
            content_lower = item.content.lower()
            if query_lower in content_lower:
                # 命中位置越靠前、置信度越高，分数越高
                pos = content_lower.find(query_lower)
                score = item.confidence * (1.0 / (1.0 + pos / 100.0))
                scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [item for _, item in scored[:limit]]
        for item in results:
            item.touch()
        return results

    def all_items(self) -> List[KnowledgeItem]:
        """返回所有知识条目（按更新时间倒序）。"""
        return sorted(self._knowledge.values(),
                      key=lambda x: x.updated_at, reverse=True)

    def get_progress(self) -> LearningProgress:
        """获取学习进度。"""
        self._update_mastery()
        return self._progress

    def _update_mastery(self) -> None:
        """根据知识库整体置信度更新掌握程度。"""
        if not self._knowledge:
            self._progress.mastery_level = 0.0
            return
        total_conf = sum(item.confidence for item in self._knowledge.values())
        avg_conf = total_conf / len(self._knowledge)
        # 掌握程度 = 平均置信度 * 覆盖率（相对最大容量的对数饱和）
        coverage = math.log1p(len(self._knowledge)) / math.log1p(self._max_items)
        self._progress.mastery_level = round(avg_conf * coverage, 4)

    def stats(self) -> Dict[str, Any]:
        """返回学习器统计信息。"""
        return {
            "total_items": len(self._knowledge),
            "capacity": self._max_items,
            "avg_confidence": (
                sum(i.confidence for i in self._knowledge.values()) / len(self._knowledge)
                if self._knowledge else 0.0
            ),
            "mastery_level": self._progress.mastery_level,
            "total_learned": self._progress.total_learned,
        }


# ============================================================
# 2. 不确定性采样（UncertaintySampler）
# ============================================================

@dataclass
class UncertaintyScore:
    """不确定性评分结果。"""

    question: str
    uncertainty: float  # 0..1，越高越不确定
    confidence: float  # 0..1，模型置信度
    entropy: float  # 信息熵估计
    reasons: List[str] = field(default_factory=list)


@dataclass
class QuestionCandidate:
    """候选问题（待学习）。"""

    question: str
    value_score: float  # 学习价值评分 0..1
    uncertainty: float
    priority: int  # 优先级，数值越大越优先
    source: str = "conversation"


class UncertaintySampler:
    """不确定性采样器 — 评估模型对问题的不确定性，挑选最有价值的问题。

    采用启发式特征：
      - 问题长度与复杂度
      - 是否包含不确定/模糊标记词
      - 模型给出的置信度（若提供）
      - 是否涉及知识库中未覆盖的领域
    """

    # 不确定性标记词
    UNCERTAIN_MARKERS = [
        "不确定", "不清楚", "可能", "也许", "大概", "似乎", "好像",
        "maybe", "perhaps", "might", "could be", "not sure", "unclear",
        "i think", "guess", "估计", "猜测", "应该",
    ]
    # 复杂度标记词
    COMPLEX_MARKERS = [
        "为什么", "如何", "原理", "区别", "对比", "分析", "证明",
        "why", "how", "explain", "compare", "analyze", "prove",
    ]

    def __init__(self, learner: Optional[ContinuousLearner] = None) -> None:
        self._learner = learner

    def estimate_uncertainty(self, question: str, answer: str = "",
                             model_confidence: Optional[float] = None) -> UncertaintyScore:
        """评估单个问题的不确定性。"""
        reasons: List[str] = []
        q_lower = question.lower()
        a_lower = answer.lower()

        # 特征1：不确定性标记词
        uncertain_hits = sum(1 for m in self.UNCERTAIN_MARKERS if m in a_lower or m in q_lower)
        marker_score = min(1.0, uncertain_hits * 0.2)
        if uncertain_hits > 0:
            reasons.append(f"含 {uncertain_hits} 个不确定标记词")

        # 特征2：问题复杂度
        complex_hits = sum(1 for m in self.COMPLEX_MARKERS if m in q_lower)
        complexity_score = min(1.0, complex_hits * 0.25)

        # 特征3：问题长度（长问题通常更难）
        length_score = min(1.0, len(question) / 200.0)

        # 特征4：知识库覆盖度（若提供 learner）
        coverage_score = 0.0
        if self._learner is not None:
            hits = self._learner.search(question, limit=1)
            if not hits:
                coverage_score = 0.6
                reasons.append("知识库未覆盖该问题领域")
            else:
                coverage_score = max(0.0, 1.0 - hits[0].confidence)
                if hits[0].confidence < 0.5:
                    reasons.append("相关知识置信度偏低")

        # 特征5：模型置信度（若提供）
        model_score = 0.0
        if model_confidence is not None:
            model_score = 1.0 - max(0.0, min(1.0, model_confidence))
            reasons.append(f"模型置信度={model_confidence:.2f}")

        # 加权融合
        uncertainty = (
            marker_score * 0.25
            + complexity_score * 0.20
            + length_score * 0.10
            + coverage_score * 0.25
            + model_score * 0.20
        )
        uncertainty = max(0.0, min(1.0, uncertainty))
        confidence = 1.0 - uncertainty
        # 信息熵近似：不确定性越高熵越大
        entropy = -(
            uncertainty * math.log2(uncertainty + 1e-9)
            + confidence * math.log2(confidence + 1e-9)
        )

        return UncertaintyScore(
            question=question,
            uncertainty=round(uncertainty, 4),
            confidence=round(confidence, 4),
            entropy=round(entropy, 4),
            reasons=reasons,
        )

    def compute_confidence(self, question: str, answer: str = "",
                           model_confidence: Optional[float] = None) -> float:
        """计算模型对单个问题的置信度（0..1）。"""
        score = self.estimate_uncertainty(question, answer, model_confidence)
        return score.confidence

    def select_valuable_questions(self, questions: List[str],
                                  answers: Optional[List[str]] = None,
                                  top_k: int = 5) -> List[QuestionCandidate]:
        """从候选问题中选择最有学习价值的若干个。

        学习价值 = 不确定性 * (1 + 复杂度加权)
        """
        answers = answers or [""] * len(questions)
        candidates: List[QuestionCandidate] = []
        for q, a in zip(questions, answers):
            score = self.estimate_uncertainty(q, a)
            # 复杂度加权
            q_lower = q.lower()
            complex_hits = sum(1 for m in self.COMPLEX_MARKERS if m in q_lower)
            complexity = min(1.0, complex_hits * 0.25)
            value = score.uncertainty * (0.6 + 0.4 * complexity)
            value = max(0.0, min(1.0, value))
            # 优先级：value * 10 取整
            priority = int(round(value * 10))
            candidates.append(QuestionCandidate(
                question=q,
                value_score=round(value, 4),
                uncertainty=score.uncertainty,
                priority=priority,
            ))
        # 按价值降序
        candidates.sort(key=lambda c: c.value_score, reverse=True)
        return candidates[:top_k]


# ============================================================
# 3. 知识冲突检测（ConflictDetector）
# ============================================================

@dataclass
class Conflict:
    """知识冲突记录。"""

    id: str
    new_content: str
    existing_id: str
    existing_content: str
    conflict_type: str  # contradiction / overlap / outdated
    severity: float  # 0..1
    description: str = ""
    detected_at: float = field(default_factory=time.time)


@dataclass
class ConflictResolution:
    """冲突解决结果。"""

    conflict_id: str
    strategy: str  # keep_newest / keep_highest_confidence / merge / manual_review
    kept_id: Optional[str]
    merged_content: Optional[str] = None
    resolved_at: float = field(default_factory=time.time)
    note: str = ""


class ConflictDetector:
    """知识冲突检测器 — 检测新知识与已有知识的冲突并提供解决策略。

    冲突类型：
      - contradiction：直接矛盾（含互斥关键词）
      - overlap：高度重叠（语义近似）
      - outdated：旧知识可能过时
    """

    # 互斥/否定关键词对（简化版）
    CONTRADICTION_PAIRS = [
        ("是", "不是"), ("对", "错"), ("正确", "错误"),
        ("可以", "不可以"), ("能", "不能"), ("会", "不会"),
        ("yes", "no"), ("true", "false"), ("是", "否"),
        ("成功", "失败"), ("增加", "减少"), ("上升", "下降"),
    ]

    def __init__(self, learner: Optional[ContinuousLearner] = None,
                 overlap_threshold: float = 0.8) -> None:
        self._learner = learner
        self._overlap_threshold = overlap_threshold
        self._conflicts: Dict[str, Conflict] = {}
        self._resolutions: List[ConflictResolution] = []

    def detect(self, new_content: str,
               existing_items: Optional[List[KnowledgeItem]] = None) -> List[Conflict]:
        """检测新内容与已有知识的冲突。"""
        if not new_content.strip():
            return []
        # 若未提供候选集，则从 learner 按字符 bigram 重叠检索相关项
        if existing_items is None and self._learner is not None:
            existing_items = self._find_candidates(new_content, limit=5)
        existing_items = existing_items or []

        conflicts: List[Conflict] = []
        new_lower = new_content.lower()

        for item in existing_items:
            existing_lower = item.content.lower()
            conflict_type = ""
            severity = 0.0
            description = ""

            # 1. 矛盾检测：互斥关键词对
            #    判定规则：一方仅肯定（含正标记且不含反标记）、另一方否定 → 矛盾
            for pos, neg in self.CONTRADICTION_PAIRS:
                new_pos = pos in new_lower
                new_neg = neg in new_lower
                exist_pos = pos in existing_lower
                exist_neg = neg in existing_lower
                if (new_pos and not new_neg and exist_neg) or \
                   (new_neg and exist_pos and not exist_neg):
                    conflict_type = "contradiction"
                    severity = 0.85
                    description = f"检测到互斥标记：'{pos}' vs '{neg}'"
                    break

            # 2. 重叠检测：基于 Jaccard 相似度
            if not conflict_type:
                similarity = self._jaccard(new_lower, existing_lower)
                if similarity >= self._overlap_threshold:
                    conflict_type = "overlap"
                    severity = similarity * 0.6
                    description = f"内容高度重叠（相似度={similarity:.2f}）"

            # 3. 过时检测：旧知识长期未更新且新内容含时间标记
            if not conflict_type:
                age = time.time() - item.updated_at
                if age > 30 * 86400 and re.search(r"\d{4}年|最新|当前|现在|new|latest|current", new_lower):
                    conflict_type = "outdated"
                    severity = 0.5
                    description = f"旧知识已 {int(age / 86400)} 天未更新，可能过时"

            if conflict_type:
                conflict = Conflict(
                    id=uuid.uuid4().hex[:12],
                    new_content=new_content,
                    existing_id=item.id,
                    existing_content=item.content,
                    conflict_type=conflict_type,
                    severity=round(severity, 4),
                    description=description,
                )
                conflicts.append(conflict)
                self._conflicts[conflict.id] = conflict

        if conflicts:
            logger.info("检测到 %d 处知识冲突", len(conflicts))
        return conflicts

    @staticmethod
    def _bigrams(s: str) -> set:
        """计算字符串的字符 bigram 集合。"""
        s = re.sub(r"\s+", "", s)
        if len(s) <= 1:
            return {s} if s else set()
        return {s[i:i + 2] for i in range(len(s) - 1)}

    def _find_candidates(self, new_content: str, limit: int = 5) -> List[KnowledgeItem]:
        """从 learner 中按字符 bigram 重叠检索候选冲突项。"""
        if self._learner is None:
            return []
        new_bigrams = self._bigrams(new_content.lower())
        if not new_bigrams:
            return []
        scored: List[Tuple[int, KnowledgeItem]] = []
        for item in self._learner.all_items():
            item_bigrams = self._bigrams(item.content.lower())
            overlap = len(new_bigrams & item_bigrams)
            if overlap > 0:
                scored.append((overlap, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:limit]]

    @staticmethod
    def _jaccard(a: str, b: str) -> float:
        """计算两个字符串的 Jaccard 相似度（基于字符 bigram）。"""
        sa, sb = ConflictDetector._bigrams(a), ConflictDetector._bigrams(b)
        if not sa and not sb:
            return 1.0
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    def resolve(self, conflict: Conflict,
                strategy: str = "keep_highest_confidence",
                new_confidence: float = 0.5) -> ConflictResolution:
        """按指定策略解决冲突。"""
        kept_id: Optional[str] = None
        merged_content: Optional[str] = None
        note = ""

        if strategy == "keep_newest":
            # 保留新知识：降低旧知识置信度（新内容交由 learner 学习）
            kept_id = None
            if self._learner is not None:
                self._learner.update_knowledge(conflict.existing_id, confidence=0.1)
            note = "保留新知识，降低旧知识置信度"

        elif strategy == "keep_highest_confidence":
            # 保留置信度更高的一方
            if self._learner is not None:
                existing = self._learner.get(conflict.existing_id)
                existing_conf = existing.confidence if existing else 0.0
            else:
                existing_conf = 0.5
            if new_confidence >= existing_conf:
                kept_id = None
                if self._learner is not None:
                    self._learner.update_knowledge(conflict.existing_id, confidence=0.1)
                note = f"新知识置信度({new_confidence:.2f})≥旧({existing_conf:.2f})，保留新知识"
            else:
                kept_id = conflict.existing_id
                note = f"旧知识置信度({existing_conf:.2f})>新({new_confidence:.2f})，保留旧知识"

        elif strategy == "merge":
            # 合并：拼接两者内容
            merged_content = f"{conflict.existing_content}\n[更新] {conflict.new_content}"
            if self._learner is not None:
                self._learner.update_knowledge(
                    conflict.existing_id, content=merged_content,
                    confidence=max(new_confidence, 0.6),
                )
            kept_id = conflict.existing_id
            note = "合并新旧知识"

        else:
            # manual_review：仅标记，不自动处理
            strategy = "manual_review"
            note = "需人工审核"

        resolution = ConflictResolution(
            conflict_id=conflict.id,
            strategy=strategy,
            kept_id=kept_id,
            merged_content=merged_content,
            note=note,
        )
        self._resolutions.append(resolution)
        logger.info("冲突 %s 已解决（策略=%s）", conflict.id, strategy)
        return resolution

    def get_conflicts(self, unresolved_only: bool = False) -> List[Conflict]:
        """获取冲突列表。"""
        resolved_ids = {r.conflict_id for r in self._resolutions}
        items = list(self._conflicts.values())
        if unresolved_only:
            items = [c for c in items if c.id not in resolved_ids]
        return sorted(items, key=lambda c: c.severity, reverse=True)

    def stats(self) -> Dict[str, Any]:
        """返回冲突检测统计。"""
        by_type: Dict[str, int] = {}
        for c in self._conflicts.values():
            by_type[c.conflict_type] = by_type.get(c.conflict_type, 0) + 1
        return {
            "total_conflicts": len(self._conflicts),
            "resolved": len(self._resolutions),
            "by_type": by_type,
        }


# ============================================================
# 4. 技能总结器（SkillSummarizer）
# ============================================================

@dataclass
class SkillSummary:
    """技能总结条目。"""

    id: str
    name: str
    description: str
    triggers: List[str] = field(default_factory=list)
    steps: List[str] = field(default_factory=list)
    source_turn: str = ""
    created_at: float = field(default_factory=time.time)
    usage_count: int = 0
    last_used: Optional[float] = None


@dataclass
class SkillIndex:
    """技能索引。"""

    skills: Dict[str, SkillSummary] = field(default_factory=dict)
    total_count: int = 0
    last_updated: float = field(default_factory=time.time)


class SkillSummarizer:
    """技能总结器 — 自动总结对话中的技能和经验。

    职责：
      - 从对话回合中提取可复用的技能/经验
      - 技能沉淀与索引
      - 按关键词检索技能
    """

    # 技能触发特征：含代码块、步骤列表、命令等
    SKILL_PATTERNS = [
        (r"```[\s\S]*?```", "code_block"),
        (r"(?m)^\s*[-*]\s+", "list_items"),
        (r"(?m)^\s*\d+[.、)]\s+", "numbered_steps"),
        (r"(?:步骤|方法|流程|操作)\s*[:：]", "explicit_steps"),
    ]

    def __init__(self) -> None:
        self._index = SkillIndex()

    def summarize(self, question: str, answer: str,
                  source_turn: str = "") -> Optional[SkillSummary]:
        """从单个对话回合总结技能。

        仅当回答具备可复用结构（代码块/步骤/列表）时才生成技能。
        """
        if not answer or len(answer) < 50:
            return None

        matched_kinds: List[str] = []
        for pattern, kind in self.SKILL_PATTERNS:
            if re.search(pattern, answer):
                matched_kinds.append(kind)

        if not matched_kinds:
            return None

        # 提取技能名：取问题中较长的实词
        name_words = re.findall(r"[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}", question)
        name = name_words[0] if name_words else "未命名技能"

        # 提取触发词
        triggers = [w for w in name_words[:5]]
        # 提取步骤：按行解析编号/列表项
        steps = self._extract_steps(answer)

        skill_id = uuid.uuid4().hex[:12]
        skill = SkillSummary(
            id=skill_id,
            name=name,
            description=question[:200],
            triggers=triggers,
            steps=steps,
            source_turn=source_turn,
        )
        self._index.skills[skill_id] = skill
        self._index.total_count = len(self._index.skills)
        self._index.last_updated = time.time()
        logger.info("沉淀技能 %s（名称=%s，触发词=%d，步骤=%d）",
                    skill_id, name, len(triggers), len(steps))
        return skill

    @staticmethod
    def _extract_steps(answer: str) -> List[str]:
        """从回答中提取步骤列表。"""
        steps: List[str] = []
        # 编号步骤：1. xxx / 1、xxx / 1) xxx
        for m in re.finditer(r"(?m)^\s*\d+[.、)]\s+(.+)$", answer):
            step = m.group(1).strip()
            if step and step not in steps:
                steps.append(step)
        if steps:
            return steps[:20]
        # 列表项：- xxx / * xxx
        for m in re.finditer(r"(?m)^\s*[-*]\s+(.+)$", answer):
            step = m.group(1).strip()
            if step and step not in steps:
                steps.append(step)
        return steps[:20]

    def index_skill(self, skill: SkillSummary) -> None:
        """将外部技能加入索引。"""
        self._index.skills[skill.id] = skill
        self._index.total_count = len(self._index.skills)
        self._index.last_updated = time.time()

    def search_skills(self, query: str, limit: int = 5) -> List[SkillSummary]:
        """按关键词检索技能。"""
        query_words = set(w.lower() for w in re.findall(r"\w{2,}", query))
        if not query_words:
            return []
        scored: List[Tuple[int, SkillSummary]] = []
        for skill in self._index.skills.values():
            hay = f"{skill.name} {skill.description} {' '.join(skill.triggers)}".lower()
            hits = sum(1 for w in query_words if w in hay)
            if hits > 0:
                scored.append((hits, skill))
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [s for _, s in scored[:limit]]
        # 记录使用
        for s in results:
            s.usage_count += 1
            s.last_used = time.time()
        return results

    def get(self, skill_id: str) -> Optional[SkillSummary]:
        """按 id 获取技能。"""
        skill = self._index.skills.get(skill_id)
        if skill is not None:
            skill.usage_count += 1
            skill.last_used = time.time()
        return skill

    def list_skills(self) -> List[SkillSummary]:
        """列出所有技能（按使用次数倒序）。"""
        return sorted(self._index.skills.values(),
                      key=lambda s: s.usage_count, reverse=True)

    def stats(self) -> Dict[str, Any]:
        """返回技能总结统计。"""
        return {
            "total_skills": self._index.total_count,
            "total_uses": sum(s.usage_count for s in self._index.skills.values()),
            "last_updated": self._index.last_updated,
        }


# ============================================================
# 5. 记忆管理器（MemoryManager）
# ============================================================

@dataclass
class MemoryEntry:
    """记忆条目。"""

    id: str
    content: str
    strength: float = 1.0  # 记忆强度 0..1
    importance: float = 0.5  # 重要性 0..1
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    # 艾宾浩斯遗忘曲线参数：稳定度（越大遗忘越慢）
    stability: float = 1.0


@dataclass
class MemoryStrength:
    """记忆强度评估结果。"""

    memory_id: str
    strength: float  # 当前强度 0..1
    retention: float  # 记忆保持率 0..1
    stability: float  # 稳定度
    needs_review: bool  # 是否需要复习


class MemoryManager:
    """记忆管理器 — 基于遗忘曲线的记忆衰减与巩固。

    采用简化版艾宾浩斯遗忘曲线：
        R = exp(-t / S)
    其中 R 为保持率，t 为经过时间（秒），S 为稳定度。
    记忆巩固：每次访问/复习提升稳定度，重要记忆衰减更慢。
    """

    # 默认稳定度（秒）：约 1 天的衰减时间常数
    DEFAULT_STABILITY = 86400.0
    # 巩固增益：每次复习稳定度乘以该系数
    CONSOLIDATION_FACTOR = 1.6
    # 强度低于该阈值则需复习
    REVIEW_THRESHOLD = 0.3

    def __init__(self) -> None:
        self._memories: Dict[str, MemoryEntry] = {}

    def add(self, content: str, importance: float = 0.5,
            memory_id: Optional[str] = None) -> MemoryEntry:
        """添加一条记忆。"""
        mid = memory_id or uuid.uuid4().hex[:12]
        # 重要性越高，初始稳定度越大（衰减越慢）
        stability = self.DEFAULT_STABILITY * (0.5 + importance)
        entry = MemoryEntry(
            id=mid,
            content=content,
            strength=1.0,
            importance=max(0.0, min(1.0, importance)),
            stability=stability,
        )
        self._memories[mid] = entry
        logger.debug("添加记忆 %s（重要性=%.2f，稳定度=%.0f）", mid, importance, stability)
        return entry

    def access(self, memory_id: str) -> Optional[MemoryEntry]:
        """访问一条记忆（同时触发巩固）。"""
        entry = self._memories.get(memory_id)
        if entry is None:
            return None
        self.consolidate(memory_id)
        return entry

    def evaluate_strength(self, memory_id: str) -> Optional[MemoryStrength]:
        """评估记忆强度。"""
        entry = self._memories.get(memory_id)
        if entry is None:
            return None
        now = time.time()
        elapsed = max(now - entry.last_accessed, 0.0)
        # 遗忘曲线：保持率
        retention = math.exp(-elapsed / max(entry.stability, 1.0))
        # 强度 = 保持率 * 重要性加权
        strength = retention * (0.5 + 0.5 * entry.importance)
        strength = max(0.0, min(1.0, strength))
        needs_review = strength < self.REVIEW_THRESHOLD
        return MemoryStrength(
            memory_id=memory_id,
            strength=round(strength, 4),
            retention=round(retention, 4),
            stability=round(entry.stability, 2),
            needs_review=needs_review,
        )

    def decay(self, memory_id: Optional[str] = None) -> None:
        """执行记忆衰减。

        若指定 memory_id，仅衰减该条；否则衰减全部并清理过低强度者。
        """
        if memory_id is not None:
            self._decay_one(memory_id)
            return
        # 全量衰减：清理强度过低的记忆
        now = time.time()
        to_remove: List[str] = []
        for mid, entry in self._memories.items():
            elapsed = max(now - entry.last_accessed, 0.0)
            retention = math.exp(-elapsed / max(entry.stability, 1.0))
            strength = retention * (0.5 + 0.5 * entry.importance)
            # 重要记忆不轻易清除
            if strength < 0.05 and entry.importance < 0.7:
                to_remove.append(mid)
        for mid in to_remove:
            del self._memories[mid]
        if to_remove:
            logger.info("记忆衰减：清理 %d 条低强度记忆", len(to_remove))

    def _decay_one(self, memory_id: str) -> None:
        """衰减单条记忆（仅更新强度，不删除）。"""
        entry = self._memories.get(memory_id)
        if entry is None:
            return
        strength = self.evaluate_strength(memory_id)
        if strength is not None:
            entry.strength = strength.strength

    def consolidate(self, memory_id: str) -> bool:
        """记忆巩固 — 通过复习强化记忆。

        每次巩固：稳定度提升、强度恢复、访问计数+1。
        重要记忆巩固效果更强。
        """
        entry = self._memories.get(memory_id)
        if entry is None:
            return False
        # 稳定度提升（受重要性影响）
        boost = self.CONSOLIDATION_FACTOR * (0.7 + 0.3 * entry.importance)
        entry.stability *= boost
        entry.strength = 1.0
        entry.access_count += 1
        entry.last_accessed = time.time()
        logger.debug("巩固记忆 %s（稳定度=%.0f，访问=%d）",
                     memory_id, entry.stability, entry.access_count)
        return True

    def get_review_candidates(self, limit: int = 10) -> List[MemoryEntry]:
        """获取需要复习的记忆（强度低于阈值）。"""
        candidates: List[Tuple[float, MemoryEntry]] = []
        for mid in self._memories:
            strength = self.evaluate_strength(mid)
            if strength is not None and strength.needs_review:
                candidates.append((strength.strength, self._memories[mid]))
        # 强度最低的优先
        candidates.sort(key=lambda x: x[0])
        return [entry for _, entry in candidates[:limit]]

    def search(self, query: str, limit: int = 5) -> List[MemoryEntry]:
        """关键词搜索记忆（按强度加权）。"""
        query_lower = query.lower()
        scored: List[Tuple[float, MemoryEntry]] = []
        for entry in self._memories.values():
            if query_lower in entry.content.lower():
                strength = self.evaluate_strength(entry.id)
                s = strength.strength if strength else entry.strength
                scored.append((s, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [e for _, e in scored[:limit]]
        for e in results:
            self.consolidate(e.id)
        return results

    def stats(self) -> Dict[str, Any]:
        """返回记忆管理统计。"""
        if not self._memories:
            return {"total_memories": 0, "avg_strength": 0.0, "needs_review": 0}
        strengths = [self.evaluate_strength(mid) for mid in self._memories]
        strengths = [s for s in strengths if s is not None]
        avg_strength = sum(s.strength for s in strengths) / len(strengths) if strengths else 0.0
        needs_review = sum(1 for s in strengths if s.needs_review)
        return {
            "total_memories": len(self._memories),
            "avg_strength": round(avg_strength, 4),
            "needs_review": needs_review,
        }


# ============================================================
# 6. 自我评估器（SelfEvaluator）
# ============================================================

@dataclass
class CapabilityBoundary:
    """能力边界评估。"""

    domain: str
    confidence: float  # 自评置信度 0..1
    sample_count: int  # 样本数
    success_rate: float  # 成功率 0..1
    last_evaluated: float = field(default_factory=time.time)


@dataclass
class KnowledgeGap:
    """知识盲区。"""

    topic: str
    gap_score: float  # 盲区程度 0..1，越高越盲
    related_questions: List[str] = field(default_factory=list)
    priority: int = 0


@dataclass
class LearningPlan:
    """学习计划。"""

    gaps: List[KnowledgeGap] = field(default_factory=list)
    priorities: List[str] = field(default_factory=list)
    suggested_questions: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


class SelfEvaluator:
    """自我评估器 — 评估能力边界、识别盲区、生成学习计划。

    依据：
      - 持续学习器的知识覆盖与置信度
      - 不确定性采样器的高不确定性问题
      - 历史成功率（若提供）
    """

    def __init__(self, learner: Optional[ContinuousLearner] = None,
                 sampler: Optional[UncertaintySampler] = None) -> None:
        self._learner = learner
        self._sampler = sampler
        self._capabilities: Dict[str, CapabilityBoundary] = {}
        self._pending_questions: List[str] = []  # 待学习的高价值问题

    def record_question(self, question: str) -> None:
        """记录一个待评估的问题。"""
        if question and question.strip():
            self._pending_questions.append(question.strip())
            # 控制队列长度
            if len(self._pending_questions) > 500:
                self._pending_questions = self._pending_questions[-500:]

    def evaluate_capability(self, domain: str, sample_count: int = 0,
                            success_rate: float = 0.0) -> CapabilityBoundary:
        """评估指定领域的能力边界。"""
        # 综合置信度：知识库覆盖 + 历史成功率
        knowledge_conf = 0.5
        if self._learner is not None:
            items = self._learner.all_items()
            domain_items = [i for i in items if domain.lower() in i.content.lower()
                            or domain.lower() in " ".join(i.tags).lower()]
            if domain_items:
                knowledge_conf = sum(i.confidence for i in domain_items) / len(domain_items)
            else:
                knowledge_conf = 0.1  # 未覆盖领域置信度低

        # 样本数加权：样本越多置信度越可靠
        sample_factor = min(1.0, sample_count / 20.0) if sample_count > 0 else 0.0
        confidence = knowledge_conf * 0.6 + success_rate * 0.4
        confidence = confidence * (0.5 + 0.5 * sample_factor) if sample_count > 0 else confidence * 0.5

        cap = CapabilityBoundary(
            domain=domain,
            confidence=round(max(0.0, min(1.0, confidence)), 4),
            sample_count=sample_count,
            success_rate=success_rate,
        )
        self._capabilities[domain] = cap
        logger.info("能力评估 [%s]：置信度=%.2f，样本=%d，成功率=%.2f",
                    domain, cap.confidence, sample_count, success_rate)
        return cap

    def identify_gaps(self, top_k: int = 5) -> List[KnowledgeGap]:
        """识别知识盲区。"""
        gaps: List[KnowledgeGap] = []

        # 盲区1：能力置信度低的领域
        for domain, cap in self._capabilities.items():
            if cap.confidence < 0.5:
                gap_score = 1.0 - cap.confidence
                gaps.append(KnowledgeGap(
                    topic=domain,
                    gap_score=round(gap_score, 4),
                    related_questions=[],
                    priority=int(round(gap_score * 10)),
                ))

        # 盲区2：高不确定性的待学习问题
        if self._sampler is not None and self._pending_questions:
            candidates = self._sampler.select_valuable_questions(
                self._pending_questions, top_k=top_k
            )
            for cand in candidates:
                if cand.value_score > 0.4:
                    gaps.append(KnowledgeGap(
                        topic=cand.question[:50],
                        gap_score=cand.value_score,
                        related_questions=[cand.question],
                        priority=cand.priority,
                    ))

        # 盲区3：知识库中置信度偏低的知识
        if self._learner is not None:
            for item in self._learner.all_items():
                if item.confidence < 0.3:
                    gaps.append(KnowledgeGap(
                        topic=item.content[:50],
                        gap_score=1.0 - item.confidence,
                        related_questions=[],
                        priority=int(round((1.0 - item.confidence) * 10)),
                    ))

        # 去重与排序
        seen: set = set()
        unique: List[KnowledgeGap] = []
        for g in gaps:
            key = g.topic
            if key not in seen:
                seen.add(key)
                unique.append(g)
        unique.sort(key=lambda x: x.gap_score, reverse=True)
        return unique[:top_k]

    def generate_plan(self, top_k: int = 5) -> LearningPlan:
        """生成学习计划。"""
        gaps = self.identify_gaps(top_k=top_k)
        priorities = [g.topic for g in gaps]
        suggested: List[str] = []
        for g in gaps:
            suggested.extend(g.related_questions)
        plan = LearningPlan(
            gaps=gaps,
            priorities=priorities,
            suggested_questions=suggested[:20],
        )
        logger.info("生成学习计划：覆盖 %d 个盲区，%d 个建议问题",
                    len(gaps), len(plan.suggested_questions))
        return plan

    def get_capabilities(self) -> List[CapabilityBoundary]:
        """获取所有已评估的能力边界。"""
        return list(self._capabilities.values())

    def stats(self) -> Dict[str, Any]:
        """返回自我评估统计。"""
        avg_conf = (
            sum(c.confidence for c in self._capabilities.values()) / len(self._capabilities)
            if self._capabilities else 0.0
        )
        return {
            "evaluated_domains": len(self._capabilities),
            "avg_confidence": round(avg_conf, 4),
            "pending_questions": len(self._pending_questions),
        }


# ============================================================
# 7. ActiveLearningPlugin 插件类
# ============================================================

class ActiveLearningPlugin(Plugin):
    """主动学习与自我进化插件 — 整合持续学习、不确定性采样、冲突检测、
    技能总结、记忆管理与自我评估。

    通过事件总线订阅对话回合完成事件，自动从对话中学习；
    并在定时任务中执行记忆衰减与自我评估。
    """

    name = "active_learning"
    load_priority = 5

    def __init__(self) -> None:
        super().__init__()
        self._learner: Optional[ContinuousLearner] = None
        self._sampler: Optional[UncertaintySampler] = None
        self._conflict_detector: Optional[ConflictDetector] = None
        self._skill_summarizer: Optional[SkillSummarizer] = None
        self._memory_manager: Optional[MemoryManager] = None
        self._self_evaluator: Optional[SelfEvaluator] = None
        self._enabled: bool = True
        self._auto_learn: bool = True
        self._min_answer_length: int = 50

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("active_learning", {}) or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._auto_learn = bool(cfg.get("auto_learn", True))
        self._min_answer_length = int(cfg.get("min_answer_length", 50))

        if not self._enabled:
            logger.info("active_learning plugin disabled by config")
            return

        # 初始化各组件（组件间存在依赖：sampler/conflict_detector/self_evaluator 依赖 learner）
        max_items = int(cfg.get("max_knowledge_items", 5000))
        self._learner = ContinuousLearner(max_items=max_items)
        self._sampler = UncertaintySampler(learner=self._learner)
        self._conflict_detector = ConflictDetector(learner=self._learner)
        self._skill_summarizer = SkillSummarizer()
        self._memory_manager = MemoryManager()
        self._self_evaluator = SelfEvaluator(
            learner=self._learner, sampler=self._sampler
        )

        # 订阅事件
        if self.bus is not None:
            self.bus.subscribe("turn_completed", self._on_turn_completed)
            self.bus.subscribe("turn_failed", self._on_turn_failed)
            self.bus.subscribe("cron", self._on_cron)

        logger.info("active_learning plugin configured (auto_learn=%s)", self._auto_learn)

    async def _on_turn_completed(self, event) -> None:
        """对话回合完成：从中学习知识、总结技能、记录记忆。"""
        if not self._enabled or not self._auto_learn:
            return
        turn = event.get("turn")
        if turn is None:
            return
        question = getattr(turn, "input_text", "") or ""
        answer = getattr(turn, "result", "") or ""
        if not question or not answer or len(answer) < self._min_answer_length:
            return
        if getattr(turn, "error", None):
            return

        # 1. 记录问题供自我评估
        if self._self_evaluator is not None:
            self._self_evaluator.record_question(question)

        # 2. 评估不确定性
        uncertainty: Optional[UncertaintyScore] = None
        if self._sampler is not None:
            uncertainty = self._sampler.estimate_uncertainty(question, answer)
            # 高不确定性内容降低学习置信度
            learn_confidence = uncertainty.confidence
        else:
            learn_confidence = 0.6

        # 3. 冲突检测
        if self._conflict_detector is not None:
            conflicts = self._conflict_detector.detect(answer)
            for conflict in conflicts:
                # 自动解决：矛盾用最高置信度策略，重叠用合并策略，过时用保留最新
                strategy = {
                    "contradiction": "keep_highest_confidence",
                    "overlap": "merge",
                    "outdated": "keep_newest",
                }.get(conflict.conflict_type, "manual_review")
                self._conflict_detector.resolve(
                    conflict, strategy=strategy, new_confidence=learn_confidence
                )

        # 4. 学习新知识
        if self._learner is not None:
            content = f"Q: {question}\nA: {answer[:500]}"
            tags = self._extract_tags(question)
            self._learner.learn(
                content=content,
                source=getattr(turn, "source", "conversation"),
                confidence=learn_confidence,
                tags=tags,
            )

        # 5. 技能总结
        if self._skill_summarizer is not None:
            self._skill_summarizer.summarize(
                question=question, answer=answer,
                source_turn=getattr(turn, "turn_id", ""),
            )

        # 6. 记忆管理：记录重要记忆
        if self._memory_manager is not None:
            importance = 0.5
            if uncertainty is not None:
                # 不确定性高的内容更重要（值得复习）
                importance = 0.4 + 0.4 * uncertainty.uncertainty
            self._memory_manager.add(
                content=f"Q: {question}\nA: {answer[:300]}",
                importance=importance,
            )

    async def _on_turn_failed(self, event) -> None:
        """对话回合失败：记录为高价值学习候选。"""
        if not self._enabled:
            return
        turn = event.get("turn")
        if turn is None:
            return
        question = getattr(turn, "input_text", "") or ""
        if question and self._self_evaluator is not None:
            # 失败的问题优先记录（盲区信号）
            self._self_evaluator.record_question(question)
            logger.debug("记录失败问题供学习：%s", question[:80])

    async def _on_cron(self, event) -> None:
        """定时任务：记忆衰减、自我评估。"""
        if not self._enabled:
            return
        job_name = event.get("name") or ""
        if job_name == "active_learning_decay":
            # 记忆衰减
            if self._memory_manager is not None:
                self._memory_manager.decay()
                logger.info("主动学习：记忆衰减完成，%s", self._memory_manager.stats())
        elif job_name == "active_learning_self_eval":
            # 自我评估与学习计划生成
            if self._self_evaluator is not None:
                plan = self._self_evaluator.generate_plan()
                logger.info("主动学习：生成学习计划，%d 个盲区", len(plan.gaps))

    @staticmethod
    def _extract_tags(question: str) -> List[str]:
        """从问题中提取标签。"""
        words = re.findall(r"[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}", question)
        return list(dict.fromkeys(words))[:5]  # 去重保序

    # --------------------------------------------------------- 公共接口
    def learn(self, content: str, source: str = "manual",
              confidence: float = 0.6, tags: Optional[List[str]] = None):
        """手动学习一条知识。"""
        if self._learner is None:
            raise RuntimeError("active_learning plugin not set up")
        return self._learner.learn(content, source=source,
                                   confidence=confidence, tags=tags)

    def evaluate_uncertainty(self, question: str, answer: str = "",
                             model_confidence: Optional[float] = None):
        """评估问题的不确定性。"""
        if self._sampler is None:
            raise RuntimeError("active_learning plugin not set up")
        return self._sampler.estimate_uncertainty(question, answer, model_confidence)

    def detect_conflicts(self, content: str):
        """检测内容与已有知识的冲突。"""
        if self._conflict_detector is None:
            raise RuntimeError("active_learning plugin not set up")
        return self._conflict_detector.detect(content)

    def summarize_skill(self, question: str, answer: str):
        """从对话总结技能。"""
        if self._skill_summarizer is None:
            raise RuntimeError("active_learning plugin not set up")
        return self._skill_summarizer.summarize(question, answer)

    def get_learning_plan(self):
        """获取学习计划。"""
        if self._self_evaluator is None:
            raise RuntimeError("active_learning plugin not set up")
        return self._self_evaluator.generate_plan()

    def search_knowledge(self, query: str, limit: int = 5):
        """搜索知识库。"""
        if self._learner is None:
            return []
        return self._learner.search(query, limit=limit)

    def get_review_memories(self, limit: int = 10):
        """获取需要复习的记忆。"""
        if self._memory_manager is None:
            return []
        return self._memory_manager.get_review_candidates(limit=limit)

    def stats(self) -> Dict[str, Any]:
        """返回整体统计信息。"""
        if not self._enabled:
            return {"enabled": False}
        return {
            "enabled": True,
            "learner": self._learner.stats() if self._learner else {},
            "conflicts": self._conflict_detector.stats() if self._conflict_detector else {},
            "skills": self._skill_summarizer.stats() if self._skill_summarizer else {},
            "memory": self._memory_manager.stats() if self._memory_manager else {},
            "self_eval": self._self_evaluator.stats() if self._self_evaluator else {},
        }

    # --------------------------------------------------------- 组件访问
    def get_learner(self) -> Optional[ContinuousLearner]:
        return self._learner

    def get_sampler(self) -> Optional[UncertaintySampler]:
        return self._sampler

    def get_conflict_detector(self) -> Optional[ConflictDetector]:
        return self._conflict_detector

    def get_skill_summarizer(self) -> Optional[SkillSummarizer]:
        return self._skill_summarizer

    def get_memory_manager(self) -> Optional[MemoryManager]:
        return self._memory_manager

    def get_self_evaluator(self) -> Optional[SelfEvaluator]:
        return self._self_evaluator

    async def stop(self) -> None:
        logger.info("active_learning plugin stopped")
        await super().stop()
