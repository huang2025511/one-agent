"""Eval Framework — LLM-as-judge answer quality evaluation.

Provides:
  - Rubric-based scoring (accuracy, completeness, relevance, safety, clarity)
  - LLM-as-judge pairwise comparison
  - Benchmark runner with persistent results
  - Self-evaluation hints for the coordinator
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from core.db import create_sqlite_connection

logger = logging.getLogger(__name__)


class ScoreDimension(Enum):
    """Evaluation dimensions."""
    ACCURACY = "accuracy"          # factual correctness
    COMPLETENESS = "completeness"  # covers all aspects of the question
    RELEVANCE = "relevance"        # directly addresses the question
    SAFETY = "safety"              # no harmful / misleading content
    CLARITY = "clarity"            # well-structured, easy to understand
    EFFICIENCY = "efficiency"      # concise, no unnecessary fluff


DIMENSION_WEIGHTS = {
    ScoreDimension.ACCURACY: 0.30,
    ScoreDimension.COMPLETENESS: 0.20,
    ScoreDimension.RELEVANCE: 0.20,
    ScoreDimension.SAFETY: 0.15,
    ScoreDimension.CLARITY: 0.10,
    ScoreDimension.EFFICIENCY: 0.05,
}


@dataclass
class EvalResult:
    """Result of a single evaluation."""
    eval_id: str = ""
    question: str = ""
    answer: str = ""
    scores: Dict[str, float] = field(default_factory=dict)
    overall: float = 0.0
    judge_model: str = ""
    judge_reasoning: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "eval_id": self.eval_id,
            "question": self.question[:500],
            "answer": self.answer[:500],
            "scores": self.scores,
            "overall": round(self.overall, 3),
            "judge_model": self.judge_model,
            "judge_reasoning": self.judge_reasoning[:500],
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


@dataclass
class PairwiseResult:
    """Result of a pairwise comparison between two answers."""
    question: str = ""
    answer_a: str = ""
    answer_b: str = ""
    winner: str = ""  # "A", "B", or "tie"
    reasoning: str = ""
    confidence: float = 0.0
    timestamp: float = field(default_factory=time.time)


class EvalHarness:
    """LLM-as-judge evaluation harness.

    Evaluates agent answers against rubrics, supports pairwise comparison,
    and persists results for tracking improvement over time.
    """

    def __init__(self, db_path: str = "data/memory/eval_results.db"):
        self._conn = create_sqlite_connection(db_path)
        self._write_lock = threading.Lock()
        self._migrate()

    def _migrate(self) -> None:
        with self._write_lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS eval_results (
                    id TEXT PRIMARY KEY,
                    question TEXT,
                    answer TEXT,
                    scores TEXT DEFAULT '{}',
                    overall REAL,
                    judge_model TEXT,
                    judge_reasoning TEXT,
                    metadata TEXT DEFAULT '{}',
                    created_at REAL
                );
                CREATE TABLE IF NOT EXISTS pairwise_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question TEXT,
                    answer_a TEXT,
                    answer_b TEXT,
                    winner TEXT,
                    reasoning TEXT,
                    confidence REAL,
                    created_at REAL
                );
                CREATE TABLE IF NOT EXISTS eval_benchmarks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    avg_score REAL,
                    num_questions INTEGER,
                    model TEXT,
                    created_at REAL
                );
            """)
            self._conn.commit()

    # --------------------------------------------------- rubric evaluation

    async def evaluate(
        self,
        llm,
        question: str,
        answer: str,
        judge_model: Optional[str] = None,
        dimensions: Optional[List[ScoreDimension]] = None,
    ) -> EvalResult:
        """Evaluate a single answer against rubrics using LLM-as-judge.

        Args:
            llm: LLM provider instance
            question: the user's question
            answer: the agent's answer to evaluate
            judge_model: model to use as judge (defaults to a strong model)
            dimensions: which dimensions to score (default: all)
        """
        import uuid

        dims = dimensions or list(ScoreDimension)
        dim_names = [d.value for d in dims]

        prompt = self._build_rubric_prompt(question, answer, dims)

        try:
            resp = await llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=judge_model,
                temperature=0.0,  # deterministic judging
                max_tokens=800,
                tools=None,
                use_cache=False,
            )
            judge_text = (resp.get("text") or "").strip()
        except Exception as exc:
            logger.warning("eval judge call failed: %s", exc)
            return EvalResult(
                eval_id=uuid.uuid4().hex[:12],
                question=question,
                answer=answer,
                scores={d: 0.0 for d in dim_names},
                overall=0.0,
                judge_model=judge_model or "unknown",
                judge_reasoning=f"judge call failed: {exc}",
            )

        # Parse scores from judge output
        scores, reasoning = self._parse_scores(judge_text, dim_names)
        overall = sum(
            scores.get(d, 0.0) * DIMENSION_WEIGHTS.get(ScoreDimension(d), 0.1)
            for d in dim_names
        ) / sum(DIMENSION_WEIGHTS.get(ScoreDimension(d), 0.1) for d in dim_names)

        result = EvalResult(
            eval_id=uuid.uuid4().hex[:12],
            question=question,
            answer=answer,
            scores=scores,
            overall=round(overall, 3),
            judge_model=judge_model or "unknown",
            judge_reasoning=reasoning,
        )

        self._persist(result)
        return result

    def _build_rubric_prompt(
        self, question: str, answer: str, dimensions: List[ScoreDimension],
    ) -> str:
        dim_descriptions = {
            ScoreDimension.ACCURACY: "事实准确性 — 信息是否正确、有无编造或错误",
            ScoreDimension.COMPLETENESS: "完整性 — 是否覆盖了问题的所有方面",
            ScoreDimension.RELEVANCE: "相关性 — 是否直接回应问题，没有跑题",
            ScoreDimension.SAFETY: "安全性 — 是否包含有害、误导或不安全的内容",
            ScoreDimension.CLARITY: "清晰度 — 结构是否清晰、易于理解",
            ScoreDimension.EFFICIENCY: "简洁性 — 是否精炼、没有冗余废话",
        }

        dim_lines = ""
        for d in dimensions:
            dim_lines += f"- {d.value}: {dim_descriptions.get(d, d.value)}\n"

        return (
            "你是一个严格的 AI 回答质量评估专家。请对以下回答按维度评分（1-10分，10分最高）。\n\n"
            "评分维度：\n"
            f"{dim_lines}\n"
            "请按以下格式输出（不要输出其他内容）：\n"
            + "\n".join(f"{d.value}: <分数>/10" for d in dimensions)
            + "\n\n"
            "然后在最后一行输出：\n"
            "总体评价: <一句话评价>\n\n"
            f"【用户问题】\n{question[:2000]}\n\n"
            f"【AI 回答】\n{answer[:3000]}\n\n"
            "请评分："
        )

    def _parse_scores(
        self, text: str, dim_names: List[str],
    ) -> tuple:
        scores: Dict[str, float] = {}
        reasoning = ""

        import re
        for dim in dim_names:
            m = re.search(rf"{re.escape(dim)}[：:]\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
            if m:
                try:
                    scores[dim] = min(10.0, max(0.0, float(m.group(1))))
                except ValueError:
                    scores[dim] = 5.0
            else:
                scores[dim] = 5.0

        m = re.search(r"总体评价[：:]\s*(.+)", text)
        if m:
            reasoning = m.group(1).strip()

        return scores, reasoning

    # --------------------------------------------------- pairwise comparison

    async def compare(
        self,
        llm,
        question: str,
        answer_a: str,
        answer_b: str,
        label_a: str = "A",
        label_b: str = "B",
        judge_model: Optional[str] = None,
    ) -> PairwiseResult:
        """Compare two answers side-by-side using LLM-as-judge."""
        prompt = (
            "你是一个 AI 回答质量评估专家。请比较以下两个回答，判断哪个更好。\n\n"
            f"【用户问题】\n{question[:2000]}\n\n"
            f"【回答 A】\n{answer_a[:3000]}\n\n"
            f"【回答 B】\n{answer_b[:3000]}\n\n"
            "请从准确性、完整性、相关性、清晰度等方面综合比较。\n"
            "输出格式：\n"
            "胜出: A / B / 平局\n"
            "理由: <一句话解释>\n"
            "置信度: <0.0-1.0>"
        )

        try:
            resp = await llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=judge_model,
                temperature=0.0,
                max_tokens=300,
                tools=None,
                use_cache=False,
            )
            judge_text = (resp.get("text") or "").strip()
        except Exception as exc:
            logger.warning("pairwise comparison failed: %s", exc)
            return PairwiseResult(
                question=question, answer_a=answer_a, answer_b=answer_b,
                winner="tie", reasoning=f"judge failed: {exc}",
            )

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

        result = PairwiseResult(
            question=question, answer_a=answer_a, answer_b=answer_b,
            winner=winner, reasoning=reasoning, confidence=confidence,
        )

        self._persist_pairwise(result)
        return result

    # --------------------------------------------------- benchmark

    async def run_benchmark(
        self,
        llm,
        questions: List[str],
        get_answer,
        judge_model: Optional[str] = None,
        name: str = "auto",
        parallel: int = 3,
    ) -> Dict[str, Any]:
        """Run a benchmark: get answers for all questions, then evaluate.

        Args:
            llm: LLM provider for judging
            questions: list of questions to test
            get_answer: async callable(question) -> answer string
            judge_model: model to use as judge
            name: benchmark name
            parallel: max parallel evaluations
        """
        import uuid

        # Phase 1: get answers (sequential or parallel)
        answers: List[str] = []
        for q in questions:
            try:
                ans = await get_answer(q)
                answers.append(ans)
            except Exception as exc:
                logger.warning("benchmark: answer failed for '%s': %s", q[:50], exc)
                answers.append(f"[error: {exc}]")

        # Phase 2: evaluate with concurrency limit
        sem = asyncio.Semaphore(parallel)

        async def evaluate_one(q: str, a: str) -> EvalResult:
            async with sem:
                return await self.evaluate(llm, q, a, judge_model)

        tasks = [evaluate_one(q, a) for q, a in zip(questions, answers)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid_results = [r for r in results if isinstance(r, EvalResult)]
        avg_score = (
            sum(r.overall for r in valid_results) / len(valid_results)
            if valid_results else 0.0
        )

        benchmark_id = await asyncio.to_thread(
            self._persist_benchmark, name, avg_score, len(valid_results), judge_model or "",
        )

        return {
            "benchmark_id": benchmark_id,
            "name": name,
            "num_questions": len(valid_results),
            "avg_score": round(avg_score, 3),
            "dimension_avgs": self._compute_dimension_avgs(valid_results),
            "results": [r.to_dict() for r in valid_results],
        }

    def _compute_dimension_avgs(self, results: List[EvalResult]) -> Dict[str, float]:
        if not results:
            return {}
        all_dims = set()
        for r in results:
            all_dims.update(r.scores.keys())
        avgs = {}
        for dim in all_dims:
            vals = [r.scores.get(dim, 0) for r in results]
            avgs[dim] = round(sum(vals) / len(vals), 3) if vals else 0.0
        return avgs

    # --------------------------------------------------- quick self-eval

    def build_self_eval_prompt(self, question: str, answer: str) -> str:
        """Build a quick self-evaluation prompt for the coordinator to use
        as a post-generation quality check (lightweight, no separate judge call)."""
        return (
            "【内部自检 — 不要输出给用户】\n\n"
            "请快速检查你的回答是否满足以下标准：\n"
            "1. 是否直接回答了用户问题？（没有跑题）\n"
            "2. 是否有事实错误或编造信息？（如有，请标注）\n"
            "3. 是否遗漏了重要方面？（如有，请补充）\n"
            "4. 语言是否清晰易懂？\n\n"
            f"用户问题：{question[:500]}\n"
            f"你的回答：{answer[:1000]}\n\n"
            "如果发现问题，请在此修正后重新输出完整回答。如果没有问题，输出 OK。"
        )

    # --------------------------------------------------- persistence

    def _persist(self, result: EvalResult) -> None:
        with self._write_lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO eval_results
                   (id, question, answer, scores, overall, judge_model, judge_reasoning, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.eval_id, result.question[:500], result.answer[:500],
                    json.dumps(result.scores, ensure_ascii=False), result.overall,
                    result.judge_model, result.judge_reasoning[:500],
                    json.dumps(result.metadata, ensure_ascii=False), result.timestamp,
                ),
            )
            self._conn.commit()

    def _persist_pairwise(self, result: PairwiseResult) -> None:
        with self._write_lock:
            self._conn.execute(
                """INSERT INTO pairwise_results
                   (question, answer_a, answer_b, winner, reasoning, confidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.question[:500], result.answer_a[:500], result.answer_b[:500],
                    result.winner, result.reasoning, result.confidence, result.timestamp,
                ),
            )
            self._conn.commit()

    def _persist_benchmark(
        self, name: str, avg_score: float, num_questions: int, model: str,
    ) -> int:
        with self._write_lock:
            cur = self._conn.execute(
                """INSERT INTO eval_benchmarks (name, avg_score, num_questions, model, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, avg_score, num_questions, model, time.time()),
            )
            self._conn.commit()
            return cur.lastrowid

    def get_recent_evals(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._write_lock:
            cur = self._conn.execute(
                "SELECT * FROM eval_results ORDER BY created_at DESC LIMIT ?", (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_benchmark_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._write_lock:
            cur = self._conn.execute(
                "SELECT * FROM eval_benchmarks ORDER BY created_at DESC LIMIT ?", (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def get_stats(self) -> Dict[str, Any]:
        with self._write_lock:
            total = self._conn.execute("SELECT COUNT(*) FROM eval_results").fetchone()[0]
            avg = self._conn.execute("SELECT AVG(overall) FROM eval_results").fetchone()[0] or 0.0
            recent = self._conn.execute(
                "SELECT AVG(overall) FROM eval_results WHERE created_at > CAST(strftime('%s','now') AS REAL) - 86400"
            ).fetchone()[0] or 0.0
        return {
            "total_evals": total,
            "avg_score": round(avg, 3),
            "avg_score_24h": round(recent, 3),
        }

    def close(self) -> None:
        try:
            if self._conn:
                self._conn.close()
                self._conn = None
        except Exception:
            pass


# Singleton
_eval_harness: Optional[EvalHarness] = None


def get_eval_harness() -> EvalHarness:
    """Get the shared EvalHarness instance."""
    global _eval_harness
    if _eval_harness is None:
        _eval_harness = EvalHarness()
    return _eval_harness