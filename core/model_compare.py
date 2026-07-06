"""Model Comparison — A/B testing for model answers.

Provides:
  - Side-by-side answer comparison: run the same prompt through two models
  - Automatic preference detection via LLM-as-judge
  - Latency and cost comparison
  - Persistent comparison history
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ModelAnswer:
    """A single model's answer with metadata."""
    model: str
    answer: str
    tokens_used: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    error: str = ""


@dataclass
class CompareResult:
    """Result of comparing two model answers."""
    question: str = ""
    model_a: ModelAnswer = field(default_factory=lambda: ModelAnswer(model=""))
    model_b: ModelAnswer = field(default_factory=lambda: ModelAnswer(model=""))
    winner: str = ""  # "A", "B", "tie"
    reasoning: str = ""
    confidence: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question[:300],
            "model_a": self.model_a.model,
            "answer_a": self.model_a.answer[:300],
            "tokens_a": self.model_a.tokens_used,
            "cost_a": self.model_a.cost_usd,
            "latency_a_ms": self.model_a.latency_ms,
            "model_b": self.model_b.model,
            "answer_b": self.model_b.answer[:300],
            "tokens_b": self.model_b.tokens_used,
            "cost_b": self.model_b.cost_usd,
            "latency_b_ms": self.model_b.latency_ms,
            "winner": self.winner,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
        }


class ModelComparer:
    """Compare answers from different models side-by-side.

    Usage:
        comparer = ModelComparer(llm, skills)
        result = await comparer.compare(
            question="What is quantum computing?",
            model_a="openai/gpt-4o",
            model_b="anthropic/claude-3.5-sonnet",
        )
    """

    def __init__(self, llm_provider, skills_manager=None):
        self._llm = llm_provider
        self._skills = skills_manager
        # Default judge model — should be a strong, unbiased model
        self._judge_model = "openai/gpt-4o"

    # --------------------------------------------------- main compare

    async def compare(
        self,
        question: str,
        model_a: str,
        model_b: str,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        judge_model: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ) -> CompareResult:
        """Compare two models' answers to the same question.

        Args:
            question: the question to ask both models
            model_a: first model identifier
            model_b: second model identifier
            temperature: LLM temperature
            max_tokens: max tokens for each answer
            judge_model: model to use as judge (default: gpt-4o)
            system_prompt: optional system prompt override
        """
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": question})

        # Get answers from both models in parallel
        answer_a, answer_b = await asyncio.gather(
            self._get_answer(messages, model_a, temperature, max_tokens),
            self._get_answer(messages, model_b, temperature, max_tokens),
            return_exceptions=True,
        )

        if isinstance(answer_a, Exception):
            answer_a = ModelAnswer(model=model_a, answer="", error=str(answer_a))
        if isinstance(answer_b, Exception):
            answer_b = ModelAnswer(model=model_b, answer="", error=str(answer_b))

        result = CompareResult(
            question=question,
            model_a=answer_a,
            model_b=answer_b,
        )

        # Judge: which answer is better?
        if answer_a.answer and answer_b.answer:
            judge = await self._judge(
                question, answer_a.answer, answer_b.answer,
                model_a, model_b, judge_model or self._judge_model,
            )
            result.winner = judge.get("winner", "tie")
            result.reasoning = judge.get("reasoning", "")
            result.confidence = judge.get("confidence", 0.5)
        elif answer_a.answer:
            result.winner = "A"
            result.reasoning = f"模型 B ({model_b}) 调用失败: {answer_b.error}"
        elif answer_b.answer:
            result.winner = "B"
            result.reasoning = f"模型 A ({model_a}) 调用失败: {answer_a.error}"
        else:
            result.winner = "tie"
            result.reasoning = "两个模型均调用失败"

        return result

    async def _get_answer(
        self, messages: list, model: str, temperature: float, max_tokens: int,
    ) -> ModelAnswer:
        """Get an answer from a specific model."""
        from models.tiers import MODEL_COST

        start = time.time()
        try:
            resp = await self._llm.chat_completion(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                tools=None,
                use_cache=False,
            )
            answer = (resp.get("text") or "").strip()
            tokens = int(resp.get("tokens_used") or 0)
            cost = (tokens / 1000) * MODEL_COST.get(model, MODEL_COST.get("default", 0.002))
            return ModelAnswer(
                model=model,
                answer=answer,
                tokens_used=tokens,
                cost_usd=round(cost, 6),
                latency_ms=(time.time() - start) * 1000,
            )
        except Exception as exc:
            return ModelAnswer(
                model=model,
                answer="",
                error=str(exc),
                latency_ms=(time.time() - start) * 1000,
            )

    async def _judge(
        self, question: str, answer_a: str, answer_b: str,
        model_a: str, model_b: str, judge_model: str,
    ) -> Dict[str, Any]:
        """Use LLM-as-judge to determine which answer is better."""
        prompt = (
            "你是一个公平的 AI 回答质量评估专家。请比较以下两个 AI 模型对同一问题的回答。\n\n"
            f"【问题】\n{question[:1500]}\n\n"
            f"【模型 A: {model_a}】\n{answer_a[:2000]}\n\n"
            f"【模型 B: {model_b}】\n{answer_b[:2000]}\n\n"
            "请从以下维度综合评价：\n"
            "1. 准确性 — 信息是否正确\n"
            "2. 完整性 — 是否全面覆盖问题\n"
            "3. 清晰度 — 表达是否清晰易懂\n"
            "4. 实用性 — 是否对用户有实际帮助\n\n"
            "输出格式：\n"
            "胜出: A / B / 平局\n"
            "理由: <2-3句话解释>\n"
            "置信度: <0.0-1.0>"
        )

        try:
            resp = await self._llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=judge_model,
                temperature=0.0,
                max_tokens=400,
                tools=None,
                use_cache=False,
            )
            judge_text = (resp.get("text") or "").strip()
        except Exception as exc:
            logger.warning("model_compare judge failed: %s", exc)
            return {"winner": "tie", "reasoning": f"judge failed: {exc}", "confidence": 0.0}

        import re

        winner = "tie"
        m = re.search(r"胜出[：:]\s*(A|B|平局)", judge_text)
        if m:
            w = m.group(1)
            winner = "A" if w == "A" else "B" if w == "B" else "tie"

        reasoning = ""
        m = re.search(r"理由[：:]\s*(.+)", judge_text)
        if m:
            reasoning = m.group(1).strip()

        confidence = 0.5
        m = re.search(r"置信度[：:]\s*(0?\.\d+|1\.0|1)", judge_text)
        if m:
            try:
                confidence = float(m.group(1))
            except ValueError:
                pass

        return {"winner": winner, "reasoning": reasoning, "confidence": confidence}

    # --------------------------------------------------- batch compare

    async def compare_multiple(
        self,
        questions: List[str],
        model_a: str,
        model_b: str,
        max_concurrency: int = 3,
        **kwargs,
    ) -> List[CompareResult]:
        """Compare two models across multiple questions."""
        sem = asyncio.Semaphore(max_concurrency)

        async def compare_one(q: str) -> CompareResult:
            async with sem:
                return await self.compare(q, model_a, model_b, **kwargs)

        tasks = [compare_one(q) for q in questions]
        return list(await asyncio.gather(*tasks, return_exceptions=True))

    def format_comparison(self, result: CompareResult) -> str:
        """Format a CompareResult for display."""
        lines = [
            f"模型对比结果",
            f"─────────────────────",
            f"问题: {result.question[:100]}",
            "",
            f"模型 A: {result.model_a.model}",
            f"  Token: {result.model_a.tokens_used} | 成本: ${result.model_a.cost_usd:.6f} | 延迟: {result.model_a.latency_ms:.0f}ms",
            f"  回答: {result.model_a.answer[:200]}...",
            "",
            f"模型 B: {result.model_b.model}",
            f"  Token: {result.model_b.tokens_used} | 成本: ${result.model_b.cost_usd:.6f} | 延迟: {result.model_b.latency_ms:.0f}ms",
            f"  回答: {result.model_b.answer[:200]}...",
            "",
            f"胜出: {result.winner}",
            f"理由: {result.reasoning}",
            f"置信度: {result.confidence:.2f}",
        ]
        return "\n".join(lines)


# Singleton
_model_comparer: Optional[ModelComparer] = None


def get_model_comparer(llm=None, skills=None) -> ModelComparer:
    """Get or create the shared ModelComparer."""
    global _model_comparer
    if _model_comparer is None and llm is not None:
        _model_comparer = ModelComparer(llm, skills)
    elif _model_comparer is None:
        _model_comparer = ModelComparer(None, None)
    return _model_comparer