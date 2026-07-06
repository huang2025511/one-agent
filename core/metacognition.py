"""Metacognition — know what you know and what you don't know.

Metacognitive capabilities:
- Confidence estimation — how sure is the agent about its answer?
- Uncertainty detection — identify when the agent is guessing
- Knowledge boundary detection — know when to say "I don't know"
- Self-assessment — evaluate answer quality before replying
- Gap awareness — identify missing information needed to answer

These help the agent produce more honest, reliable responses instead of
confidently making things up (hallucination reduction).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# Patterns that indicate uncertainty in LLM responses
_UNCERTAINTY_PATTERNS = [
    # English
    r"\bI('m| am) not sure\b",
    r"\bI think\b",
    r"\bI believe\b",
    r"\bprobably\b",
    r"\bmaybe\b",
    r"\bmight be\b",
    r"\bcould be\b",
    r"\bI guess\b",
    r"\bapproximately\b",
    r"\baround\b",
    r"\broughly\b",
    r"\bnot certain\b",
    r"\buncertain\b",
    r"\bI('m| am) not confident\b",
    # Chinese
    r"我不确定",
    r"我觉得",
    r"我认为",
    r"可能",
    r"也许",
    r"大概",
    r"大约",
    r"应该是",
    r"说不定",
    r"不太确定",
    r"我猜",
    r"似乎",
    r"貌似",
]

# Patterns that indicate hallucination / fabrication
_HALLUCINATION_RED_FLAGS = [
    # Very specific numbers without sources
    r"据统计[，,].*?%\b",
    r"研究表明[，,].*?%\b",
    r"最新数据显示",
    # Absolute certainty about uncertain topics
    r"绝对",
    r"肯定是",
    r"毫无疑问",
    r"100%",
    r"完全正确",
]

# Categories of things the agent should be cautious about
_CAUTION_CATEGORIES = {
    "medical": [
        r"诊断|治疗|药物|处方|剂量|病症|疾病|症状",
        r"diagnos|treat|medication|dosage|symptom|disease|illness",
    ],
    "legal": [
        r"法律|法规|条例|条款|合同|诉讼|律师|法院",
        r"legal|law|regulation|contract|sue|court|lawyer",
    ],
    "financial": [
        r"投资|股票|基金|理财|风险|收益|回报|利息",
        r"invest|stock|fund|financial|risk|return|interest",
    ],
    "factual": [
        r"什么时候成立|哪一年|第几届|第几条",
        r"founded in|established in|year of",
    ],
}


class MetacognitionEngine:
    """Analyzes response quality and provides self-awareness.

    The engine doesn't judge whether an answer is *correct* — it estimates
    how confident the agent should be based on:
    - Source availability (did we use tools/memory vs pure guessing?)
    - Uncertainty language in the response
    - Topic sensitivity (medical/legal/financial)
    - Factual claim specificity
    """

    def __init__(self) -> None:
        self._uncertainty_patterns = [
            re.compile(p, re.IGNORECASE) for p in _UNCERTAINTY_PATTERNS
        ]
        self._hallucination_patterns = [
            re.compile(p, re.IGNORECASE) for p in _HALLUCINATION_RED_FLAGS
        ]
        self._caution_categories: Dict[str, List[re.Pattern]] = {}
        for cat, patterns in _CAUTION_CATEGORIES.items():
            self._caution_categories[cat] = [
                re.compile(p, re.IGNORECASE) for p in patterns
            ]

    def analyze_response(
        self,
        response_text: str,
        sources_used: Optional[List[str]] = None,
        tools_used: Optional[List[str]] = None,
        question_text: str = "",
    ) -> Dict[str, Any]:
        """Analyze a response for confidence, uncertainty, and risk.

        Returns:
        {
            "confidence": 0.0-1.0,  # Estimated confidence level
            "uncertainty_signals": [...],  # Patterns found that indicate uncertainty
            "caution_topics": [...],  # Sensitive topics detected
            "hallucination_risk": "low" | "medium" | "high",
            "recommendation": "...",  # Suggested action/wording
            "source_based": True/False,  # Whether answer is based on sources
        }
        """
        sources_used = sources_used or []
        tools_used = tools_used or []
        response_text = response_text or ""

        # 1. Detect uncertainty signals
        uncertainty_signals = self._detect_uncertainty(response_text)

        # 2. Detect sensitive topics
        caution_topics = self._detect_caution_topics(question_text + " " + response_text)

        # 3. Detect hallucination red flags
        hallucination_flags = self._detect_hallucination_flags(response_text)

        # 4. Source-based confidence
        source_based = len(sources_used) > 0 or len(tools_used) > 0

        # 5. Calculate confidence
        confidence = self._calculate_confidence(
            response_text=response_text,
            sources_used=sources_used,
            tools_used=tools_used,
            uncertainty_signals=uncertainty_signals,
            caution_topics=caution_topics,
            hallucination_flags=hallucination_flags,
        )

        # 6. Determine hallucination risk
        hallucination_risk = self._assess_hallucination_risk(
            confidence=confidence,
            source_based=source_based,
            caution_topics=caution_topics,
            hallucination_flags=hallucination_flags,
        )

        # 7. Generate recommendation
        recommendation = self._generate_recommendation(
            confidence=confidence,
            hallucination_risk=hallucination_risk,
            source_based=source_based,
            caution_topics=caution_topics,
        )

        return {
            "confidence": confidence,
            "uncertainty_signals": uncertainty_signals,
            "caution_topics": caution_topics,
            "hallucination_risk": hallucination_risk,
            "hallucination_flags": hallucination_flags,
            "recommendation": recommendation,
            "source_based": source_based,
            "tools_used": tools_used,
        }

    def _detect_uncertainty(self, text: str) -> List[str]:
        """Detect linguistic signals of uncertainty."""
        signals = []
        for pattern in self._uncertainty_patterns:
            matches = pattern.findall(text)
            if matches:
                signals.extend(matches[:3])
        return list(set(signals))[:5]

    def _detect_caution_topics(self, text: str) -> List[str]:
        """Detect sensitive topics that require caution."""
        topics = []
        for category, patterns in self._caution_categories.items():
            for pattern in patterns:
                if pattern.search(text):
                    topics.append(category)
                    break
        return topics

    def _detect_hallucination_flags(self, text: str) -> List[str]:
        """Detect patterns that often accompany hallucinations."""
        flags = []
        for pattern in self._hallucination_patterns:
            matches = pattern.findall(text)
            if matches:
                flags.append(pattern.pattern[:30])
        return flags[:5]

    def _calculate_confidence(
        self,
        response_text: str,
        sources_used: List[str],
        tools_used: List[str],
        uncertainty_signals: List[str],
        caution_topics: List[str],
        hallucination_flags: List[str],
    ) -> float:
        """Calculate confidence score (0.0-1.0)."""
        score = 0.5  # Baseline

        # Source-based boost
        if sources_used:
            score += 0.2
        if tools_used:
            score += 0.15

        # Uncertainty language penalty
        score -= len(uncertainty_signals) * 0.05

        # Sensitive topic penalty
        score -= len(caution_topics) * 0.1

        # Hallucination flag penalty
        score -= len(hallucination_flags) * 0.08

        # Response length factor (very short = lower confidence)
        if len(response_text) < 20:
            score -= 0.1

        return max(0.0, min(1.0, score))

    def _assess_hallucination_risk(
        self,
        confidence: float,
        source_based: bool,
        caution_topics: List[str],
        hallucination_flags: List[str],
    ) -> str:
        """Assess risk level of hallucination."""
        risk_score = 0.0

        if not source_based:
            risk_score += 0.3
        if confidence < 0.4:
            risk_score += 0.25
        if caution_topics:
            risk_score += len(caution_topics) * 0.1
        if hallucination_flags:
            risk_score += len(hallucination_flags) * 0.1

        if risk_score >= 0.5:
            return "high"
        elif risk_score >= 0.25:
            return "medium"
        else:
            return "low"

    def _generate_recommendation(
        self,
        confidence: float,
        hallucination_risk: str,
        source_based: bool,
        caution_topics: List[str],
    ) -> str:
        """Generate a recommendation for how to present the answer."""
        recommendations = []

        if confidence < 0.3:
            recommendations.append("低置信度，建议明确说明不确定性")
        elif confidence < 0.5:
            recommendations.append("中等置信度，可适当使用限定语气")

        if hallucination_risk == "high":
            recommendations.append("高幻觉风险，建议引用来源或避免绝对化表述")
        elif hallucination_risk == "medium":
            recommendations.append("中等幻觉风险，注意核实关键信息")

        if not source_based:
            recommendations.append("未使用外部来源，建议标注为观点性内容")

        if "medical" in caution_topics:
            recommendations.append("涉及医疗话题，建议咨询专业医生")
        if "legal" in caution_topics:
            recommendations.append("涉及法律话题，建议咨询专业律师")
        if "financial" in caution_topics:
            recommendations.append("涉及金融话题，建议咨询专业顾问")

        return "; ".join(recommendations) if recommendations else "回答可信度良好"

    def format_confidence_note(
        self,
        analysis: Dict[str, Any],
        lang: str = "zh",
    ) -> str:
        """Format a confidence disclaimer for inclusion in replies.

        Only includes a note when confidence is low or risk is high.
        """
        confidence = analysis.get("confidence", 0.5)
        risk = analysis.get("hallucination_risk", "low")
        topics = analysis.get("caution_topics", [])

        if confidence > 0.6 and risk == "low" and not topics:
            return ""  # No disclaimer needed

        if lang.startswith("zh"):
            parts = ["\n\nℹ️ 说明："]
            if confidence < 0.4:
                parts.append("此回答置信度较低，仅供参考。")
            if risk == "high":
                parts.append("信息可能存在偏差，请核实后再使用。")
            if topics:
                topic_names = {
                    "medical": "医疗",
                    "legal": "法律",
                    "financial": "金融",
                    "factual": "事实",
                }
                topic_labels = [topic_names.get(t, t) for t in topics]
                parts.append(f"涉及{', '.join(topic_labels)}话题，建议咨询专业人士。")
            return " ".join(parts)
        else:
            parts = ["\n\nℹ️ Note: "]
            if confidence < 0.4:
                parts.append("Low confidence answer, for reference only.")
            if risk == "high":
                parts.append("Information may be inaccurate — please verify.")
            if topics:
                parts.append("Sensitive topic — consult a professional.")
            return " ".join(parts)

    async def evaluate_with_llm(
        self,
        llm,
        response_text: str,
        question_text: str = "",
        sources_used: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Gap 修复：用 LLM 做真正的置信度评估，替换纯正则匹配。

        之前的 _calculate_confidence 基于关键词统计（"我不确定"→扣分），
        LLM 可以流利地编造虚假答案而不触发任何关键词。现在让轻量 LLM
        真正读一遍回答内容，判断是否可信。

        返回：{"confidence": 0.0-1.0, "reason": "...", "flags": [...]}
        """
        if llm is None:
            return self._fallback_llm_eval(response_text, sources_used)

        src_info = ""
        if sources_used:
            src_info = f"使用了以下来源：{', '.join(sources_used[:5])}"

        prompt = (
            "你是答案质量评估专家。请评估以下 AI 回答的可靠性，给出 0-10 的置信度分数。\n\n"
            f"用户问题：{question_text[:300]}\n"
            f"{src_info}\n"
            f"AI 回答：{response_text[:1500]}\n\n"
            "评估标准：\n"
            "- 10分：有明确来源支撑，逻辑严密，无推测\n"
            "- 7分：合理推理，但未引用具体来源\n"
            "- 5分：回答基本合理但缺少关键细节\n"
            "- 3分：有明显推测或不确定语言\n"
            "- 1分：完全是猜测，无事实依据\n\n"
            "请只返回一个 JSON：{\"score\": 数字, \"reason\": \"简短理由\", \"flags\": [\"风险标签\"]}\n"
            "不要输出其他内容。"
        )
        try:
            resp = await llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=None,
                max_tokens=150,
                temperature=0.1,
                tools=None,
                use_cache=False,
            )
            text = (resp.get("text") or "{}").strip()
            # 提取 JSON
            import json as _json
            m = re.search(r'\{[^}]+\}', text)
            if m:
                data = _json.loads(m.group())
                score = float(data.get("score", 5)) / 10.0
                return {
                    "confidence": max(0.0, min(1.0, score)),
                    "reason": data.get("reason", ""),
                    "flags": data.get("flags", []),
                    "source": "llm",
                }
        except Exception as exc:
            logger.debug("LLM metacognition eval failed: %s", exc)
        return self._fallback_llm_eval(response_text, sources_used)

    def _fallback_llm_eval(
        self, response_text: str, sources_used: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """LLM 不可用时的回退评估（基于正则+来源）。"""
        analysis = self.analyze_response(
            response_text, sources_used=sources_used or [],
        )
        return {
            "confidence": analysis["confidence"],
            "reason": analysis.get("recommendation", "正则评估"),
            "flags": analysis.get("hallucination_flags", []),
            "source": "regex_fallback",
        }


# Singleton
_metacognition_engine: Optional[MetacognitionEngine] = None


def get_metacognition_engine() -> MetacognitionEngine:
    """Get the shared MetacognitionEngine instance."""
    global _metacognition_engine
    if _metacognition_engine is None:
        _metacognition_engine = MetacognitionEngine()
    return _metacognition_engine
