"""Deep Research — multi-step research with synthesis.

Provides:
  - Sub-question decomposition: break complex research questions into sub-questions
  - Iterative search: search for each sub-question, gather sources
  - Synthesis: combine findings into a comprehensive report
  - Source tracking: every claim is backed by a source
  - Progress reporting: real-time updates during research
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ResearchSource:
    """A source gathered during research."""
    url: str = ""
    title: str = ""
    snippet: str = ""
    relevance: float = 0.0  # 0-1


@dataclass
class ResearchFinding:
    """A finding from one sub-question."""
    sub_question: str = ""
    sources: List[ResearchSource] = field(default_factory=list)
    answer: str = ""
    confidence: float = 0.0


@dataclass
class ResearchReport:
    """Final research report."""
    main_question: str = ""
    sub_questions: List[str] = field(default_factory=list)
    findings: List[ResearchFinding] = field(default_factory=list)
    synthesis: str = ""
    sources: List[ResearchSource] = field(default_factory=list)
    duration_seconds: float = 0.0
    total_searches: int = 0


class DeepResearcher:
    """Multi-step research engine.

    Decomposes a complex question into sub-questions, searches for each,
    then synthesizes findings into a comprehensive report.
    """

    MAX_SUB_QUESTIONS = 5
    MAX_SOURCES_PER_QUESTION = 3
    MAX_SEARCH_DEPTH = 2  # how many rounds of refinement per sub-question

    def __init__(self, llm_provider, skills_manager=None):
        self._llm = llm_provider
        self._skills = skills_manager
        self._on_progress = None

    async def research(
        self,
        question: str,
        model: Optional[str] = None,
        depth: int = 2,
        on_progress=None,
    ) -> ResearchReport:
        """Execute deep research on a question.

        Args:
            question: the research question
            model: LLM model to use
            depth: research depth (1-3, higher = more thorough)
            on_progress: callback(phase, message)
        """
        self._on_progress = on_progress
        start = time.time()
        total_searches = 0

        report = ResearchReport(main_question=question)

        # Phase 1: Decompose into sub-questions
        self._emit_progress("decompose", "正在分解研究问题...")
        sub_questions = await self._decompose(question, model, depth)
        report.sub_questions = sub_questions
        logger.info("deep_research: decomposed into %d sub-questions", len(sub_questions))

        # Phase 2: Research each sub-question
        all_sources: List[ResearchSource] = []
        findings: List[ResearchFinding] = []

        for i, sq in enumerate(sub_questions):
            self._emit_progress("search", f"正在研究 ({i+1}/{len(sub_questions)}): {sq[:50]}...")
            finding, sources, searches = await self._research_sub_question(
                sq, model, depth,
            )
            findings.append(finding)
            all_sources.extend(sources)
            total_searches += searches

        report.findings = findings
        report.sources = all_sources
        report.total_searches = total_searches

        # Phase 3: Synthesize
        self._emit_progress("synthesize", "正在综合研究结果...")
        synthesis = await self._synthesize(question, findings, model)
        report.synthesis = synthesis
        report.duration_seconds = time.time() - start

        self._emit_progress("done", f"研究完成 ({len(sub_questions)} 个子问题, {total_searches} 次搜索, {report.duration_seconds:.0f}s)")
        return report

    def _emit_progress(self, phase: str, message: str) -> None:
        if self._on_progress:
            try:
                self._on_progress(phase, message)
            except Exception:
                pass

    # --------------------------------------------------- decompose

    async def _decompose(
        self, question: str, model: Optional[str], depth: int,
    ) -> List[str]:
        """Use LLM to decompose a complex question into sub-questions."""
        prompt = (
            "你是一个研究助手。请将以下复杂问题拆解成 {n} 个具体的子问题，"
            "每个子问题应该独立可研究、能通过搜索找到答案。\n\n"
            "要求：\n"
            "- 子问题要具体，不要过于宽泛\n"
            "- 按逻辑顺序排列（从背景到细节）\n"
            "- 每个子问题一行，以数字+句号开头\n"
            '- 不要包含"搜索"、"查找"等动作词\n\n'
            "【原始问题】\n{question}\n\n"
            "子问题列表："
        ).format(n=min(depth + 2, self.MAX_SUB_QUESTIONS), question=question[:2000])

        try:
            resp = await self._llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0.4,
                max_tokens=500,
                tools=None,
            )
            text = (resp.get("text") or "").strip()
        except Exception as exc:
            logger.warning("deep_research decompose failed: %s", exc)
            return [question]

        # Parse numbered list
        import re
        items = re.findall(r"\d+[\.\)、]\s*(.+)", text)
        if len(items) < 2:
            # Fallback: split by newlines
            items = [l.strip() for l in text.split("\n") if l.strip() and len(l.strip()) > 5]

        return items[:self.MAX_SUB_QUESTIONS] if items else [question]

    # --------------------------------------------------- research sub-question

    async def _research_sub_question(
        self, sub_question: str, model: Optional[str], depth: int,
    ) -> tuple:
        """Research a single sub-question: search, gather sources, answer."""
        all_sources: List[ResearchSource] = []
        total_searches = 0

        # Search rounds
        for round_num in range(min(depth, self.MAX_SEARCH_DEPTH)):
            if round_num == 0:
                search_query = sub_question
            else:
                # Refine: ask LLM what's still missing
                search_query = await self._refine_query(
                    sub_question, all_sources, model,
                )
                if not search_query:
                    break

            sources = await self._do_search(search_query)
            all_sources.extend(sources)
            total_searches += 1

            if len(all_sources) >= self.MAX_SOURCES_PER_QUESTION * 2:
                break

        # Answer the sub-question based on sources
        answer, confidence = await self._answer_from_sources(
            sub_question, all_sources, model,
        )

        finding = ResearchFinding(
            sub_question=sub_question,
            sources=all_sources,
            answer=answer,
            confidence=confidence,
        )

        return finding, all_sources, total_searches

    async def _refine_query(
        self, question: str, sources: List[ResearchSource], model: Optional[str],
    ) -> str:
        """Ask LLM what additional information is still needed."""
        if not sources:
            return question

        sources_text = "\n".join(
            f"- {s.title}: {s.snippet[:150]}" for s in sources[:3]
        )

        prompt = (
            "基于已有信息，判断是否还需要更多搜索来回答以下问题。"
            "如果信息已经足够，回复 DONE。如果需要，给出一个更精确的搜索词。\n\n"
            f"问题：{question}\n"
            f"已有信息：\n{sources_text}\n\n"
            "需要补充搜索吗？（DONE 或 搜索词）："
        )

        try:
            resp = await self._llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0.2,
                max_tokens=100,
                tools=None,
            )
            text = (resp.get("text") or "").strip()
            if "DONE" in text.upper() or "足够" in text or "不需要" in text:
                return ""
            return text[:200]
        except Exception:
            return ""

    async def _do_search(self, query: str) -> List[ResearchSource]:
        """Execute a web search and return structured sources."""
        if self._skills is None:
            return []

        try:
            web_search = self._skills.get("web_search")
            if web_search is None:
                return []

            result = await web_search.run({"input": query})
            result_text = str(result) if result else ""

            if not result_text or "无法" in result_text or "error" in result_text.lower():
                return []

            return self._parse_search_results(result_text)

        except Exception as exc:
            logger.warning("deep_research search failed: %s", exc)
            return []

    def _parse_search_results(self, text: str) -> List[ResearchSource]:
        """Parse web_search output into structured sources."""
        sources = []
        import re

        # Try to find URL + title + snippet patterns
        # Pattern 1: [title](url) — snippet
        entries = re.split(r"\n(?=\d+[\.\)])", text)
        if len(entries) < 2:
            entries = text.split("\n\n")

        for entry in entries[:self.MAX_SOURCES_PER_QUESTION]:
            entry = entry.strip()
            if not entry or len(entry) < 10:
                continue

            # Extract URL
            url_match = re.search(r'(https?://[^\s\)\]）]+)', entry)
            url = url_match.group(1) if url_match else ""

            # Extract title
            title_match = re.search(r'(?:^|\n)\s*(?:\d+[\.\)]\s*)?(?:\[([^\]]+)\]|【([^】]+)】|"([^"]+)"|《([^》]+)》)', entry)
            if title_match:
                title = next(g for g in title_match.groups() if g)
            else:
                title = entry[:80].split("\n")[0]

            # Rest is snippet
            snippet = entry
            if url:
                snippet = snippet.replace(url, "")
            if title and title in snippet:
                snippet = snippet.replace(title, "", 1)
            snippet = snippet.strip(" -:：[]()（）\n")[:300]

            sources.append(ResearchSource(
                url=url,
                title=title[:100],
                snippet=snippet,
                relevance=0.7,
            ))

        return sources

    async def _answer_from_sources(
        self, question: str, sources: List[ResearchSource], model: Optional[str],
    ) -> tuple:
        """Generate an answer based on gathered sources."""
        if not sources:
            return "未找到相关信息", 0.0

        sources_text = ""
        for i, s in enumerate(sources[:5]):
            sources_text += f"\n[来源{i+1}] {s.title}\n{s.snippet[:300]}"
            if s.url:
                sources_text += f"\nURL: {s.url}"

        prompt = (
            "基于以下搜索来源，回答用户问题。必须引用来源编号。"
            "如果信息不足，明确说明，不要编造。\n\n"
            f"问题：{question}\n"
            f"来源：{sources_text[:4000]}\n\n"
            "回答（简洁准确，引用来源）："
        )

        try:
            resp = await self._llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0.3,
                max_tokens=800,
                tools=None,
            )
            answer = (resp.get("text") or "").strip()
            # Confidence heuristic: more sources = higher confidence
            confidence = min(0.9, 0.3 + len(sources) * 0.15)
            return answer, confidence
        except Exception as exc:
            return f"回答生成失败: {exc}", 0.0

    # --------------------------------------------------- synthesize

    async def _synthesize(
        self, question: str, findings: List[ResearchFinding], model: Optional[str],
    ) -> str:
        """Synthesize all findings into a comprehensive report."""
        if not findings:
            return "研究未能产生任何有效发现。"

        findings_text = ""
        all_sources = []

        for i, f in enumerate(findings):
            findings_text += f"\n\n### 子问题 {i+1}: {f.sub_question}\n"
            findings_text += f"答案: {f.answer}\n"
            if f.sources:
                findings_text += "来源:\n"
                for j, s in enumerate(f.sources[:3]):
                    findings_text += f"  [{i+1}.{j+1}] {s.title}: {s.url}\n"
                    all_sources.append(s)

        prompt = (
            "你是一个研究报告撰写专家。请将以下研究结果整合成一篇完整的报告。\n\n"
            "要求：\n"
            "1. 开头用一段话总结核心发现\n"
            "2. 按逻辑组织内容（不要简单罗列子问题）\n"
            "3. 引用来源编号\n"
            "4. 语言专业但易懂\n"
            "5. 结尾给出结论或建议\n\n"
            f"【原始问题】\n{question}\n\n"
            f"【研究结果】{findings_text[:6000]}\n\n"
            "综合报告："
        )

        try:
            resp = await self._llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0.5,
                max_tokens=2000,
                tools=None,
            )
            return (resp.get("text") or "").strip()
        except Exception as exc:
            logger.warning("deep_research synthesize failed: %s", exc)
            # Fallback: simple concatenation
            fallback = f"# 研究报告: {question}\n\n## 核心发现\n\n"
            for i, f in enumerate(findings):
                fallback += f"### {i+1}. {f.sub_question}\n{f.answer}\n\n"
            return fallback

    # --------------------------------------------------- format

    def format_report(self, report: ResearchReport) -> str:
        """Format a ResearchReport for display."""
        lines = [
            f"深度研究报告: {report.main_question}",
            f"⏱ 耗时: {report.duration_seconds:.1f}s | 搜索: {report.total_searches}次 | 子问题: {len(report.sub_questions)}个",
            "",
            report.synthesis,
            "",
            "---",
            f"来源: {len(report.sources)} 个",
        ]
        for i, s in enumerate(report.sources[:10]):
            if s.url:
                lines.append(f"  [{i+1}] {s.title[:80]} — {s.url[:80]}")
        return "\n".join(lines)


# Singleton
_deep_researcher: Optional[DeepResearcher] = None


def get_deep_researcher(llm=None, skills=None) -> DeepResearcher:
    """Get or create the shared DeepResearcher."""
    global _deep_researcher
    if _deep_researcher is None and llm is not None:
        _deep_researcher = DeepResearcher(llm, skills)
    elif _deep_researcher is None:
        _deep_researcher = DeepResearcher(None, None)
    return _deep_researcher