"""Advanced RAG — reranking, HyDE, hybrid search for better retrieval.

Provides:
  - Result reranking: re-rank search results by semantic relevance
  - HyDE (Hypothetical Document Embeddings): generate hypothetical answer, then search
  - Hybrid search: combine keyword (BM25) + semantic (embedding) search
  - Query expansion: generate multiple query variants for broader coverage
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class AdvancedRAG:
    """Advanced retrieval-augmented generation with reranking and HyDE.

    Enhances the existing memory/embeddings system with:
    1. Result reranking using cross-encoder or LLM
    2. HyDE: generate hypothetical answer before searching
    3. Hybrid search: BM25 + embedding fusion
    4. Query expansion: broaden search coverage
    """

    def __init__(self, llm_provider=None, memory=None):
        self._llm = llm_provider
        self._memory = memory

    # --------------------------------------------------- HyDE

    async def hyde_search(
        self, query: str, top_k: int = 5, model: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """HyDE: generate a hypothetical answer, then search.

        The idea: instead of searching with the query directly, first
        generate a hypothetical answer to the query, then use that answer
        as the search query. This captures the "style" of what a good
        answer looks like, improving retrieval quality.
        """
        if not self._llm:
            # Fallback: direct search
            return await self._direct_search(query, top_k)

        # Step 1: Generate hypothetical answer
        hyde_prompt = (
            "请用一段话回答以下问题。不需要完全准确，"
            "只需要生成一个合理的假设性回答，用于帮助后续搜索。\n\n"
            f"问题：{query}\n\n"
            "假设性回答："
        )

        try:
            resp = await self._llm.chat_completion(
                messages=[{"role": "user", "content": hyde_prompt}],
                model=model,
                temperature=0.5,
                max_tokens=300,
                tools=None,
            )
            hypo_answer = (resp.get("text") or "").strip()
        except Exception as exc:
            logger.warning("hyde: generation failed: %s", exc)
            return await self._direct_search(query, top_k)

        if not hypo_answer:
            return await self._direct_search(query, top_k)

        # Step 2: Search with hypothetical answer
        logger.debug("hyde: generated hypothetical answer (%d chars)", len(hypo_answer))
        return await self._direct_search(hypo_answer, top_k * 2)

    # --------------------------------------------------- reranking

    async def rerank(
        self,
        query: str,
        results: List[Dict[str, Any]],
        top_k: int = 5,
        model: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Rerank search results by semantic relevance to the query.

        Uses LLM to score each result's relevance to the query.
        """
        if not results or not self._llm:
            return results[:top_k]

        if len(results) <= top_k:
            return results

        # Batch scoring: score all results in one LLM call
        items_text = ""
        for i, r in enumerate(results):
            content = str(r.get("content", r.get("text", "")))[:200]
            items_text += f"\n[{i}] {content}"

        prompt = (
            "对以下搜索结果按与查询的相关性排序（1-10分）。\n"
            f"查询：{query}\n"
            f"结果：{items_text[:4000]}\n\n"
            "按格式输出（只输出分数，不要其他内容）：\n"
            "0: <分数>\n1: <分数>\n..."
        )

        try:
            resp = await self._llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0.0,
                max_tokens=200,
                tools=None,
            )
            scored = self._parse_scores(resp.get("text", "") or "", len(results))
        except Exception as exc:
            logger.warning("rerank: scoring failed: %s", exc)
            return results[:top_k]

        # Sort by score descending
        scored_results = list(zip(results, scored))
        scored_results.sort(key=lambda x: x[1], reverse=True)

        return [r for r, _ in scored_results[:top_k]]

    def _parse_scores(self, text: str, count: int) -> List[float]:
        """Parse score lines like '0: 8.5'."""
        import re
        scores = [0.0] * count
        for line in text.split("\n"):
            m = re.match(r"(\d+)[：:]\s*(\d+(?:\.\d+)?)", line.strip())
            if m:
                idx = int(m.group(1))
                if 0 <= idx < count:
                    try:
                        scores[idx] = float(m.group(2))
                    except ValueError:
                        pass
        return scores

    # --------------------------------------------------- hybrid search

    async def hybrid_search(
        self, query: str, top_k: int = 5, alpha: float = 0.5,
    ) -> List[Dict[str, Any]]:
        """Hybrid search: combine BM25 (keyword) + embedding (semantic).

        alpha controls the weight: 0 = all keyword, 1 = all semantic.
        """
        # Get results from both methods
        keyword_results = await self._keyword_search(query, top_k * 2)
        semantic_results = await self._direct_search(query, top_k * 2)

        if not keyword_results and not semantic_results:
            return []

        if not keyword_results:
            return semantic_results[:top_k]
        if not semantic_results:
            return keyword_results[:top_k]

        # Reciprocal Rank Fusion (RRF)
        fused = self._rrf_fusion(
            keyword_results, semantic_results, alpha=alpha,
        )
        return fused[:top_k]

    def _rrf_fusion(
        self,
        list_a: List[Dict[str, Any]],
        list_b: List[Dict[str, Any]],
        k: int = 60,
        alpha: float = 0.5,
    ) -> List[Dict[str, Any]]:
        """Reciprocal Rank Fusion: combine two ranked lists."""
        scores: Dict[int, float] = {}
        id_to_item: Dict[int, Dict[str, Any]] = {}

        for i, item in enumerate(list_a):
            item_id = id(item)
            id_to_item[item_id] = item
            scores[item_id] = scores.get(item_id, 0) + alpha * (1.0 / (k + i + 1))

        for i, item in enumerate(list_b):
            item_id = id(item)
            id_to_item[item_id] = item
            scores[item_id] = scores.get(item_id, 0) + (1 - alpha) * (1.0 / (k + i + 1))

        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
        return [id_to_item[iid] for iid in sorted_ids if iid in id_to_item]

    # --------------------------------------------------- query expansion

    async def expand_query(
        self, query: str, model: Optional[str] = None, n_variants: int = 3,
    ) -> List[str]:
        """Generate multiple query variants for broader search coverage."""
        if not self._llm:
            return [query]

        prompt = (
            f"请为以下搜索查询生成 {n_variants} 个不同的变体，"
            "每个变体从不同角度表达同一问题。每行一个，不要编号。\n\n"
            f"原始查询：{query}\n\n"
            "变体："
        )

        try:
            resp = await self._llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0.7,
                max_tokens=200,
                tools=None,
            )
            text = (resp.get("text") or "").strip()
            variants = [l.strip() for l in text.split("\n") if l.strip()]
            return [query] + variants[:n_variants]
        except Exception:
            return [query]

    # --------------------------------------------------- keyword search (BM25-inspired)

    async def _keyword_search(
        self, query: str, top_k: int,
    ) -> List[Dict[str, Any]]:
        """Simple keyword-based search (TF-IDF inspired)."""
        if not self._memory:
            return []

        # Get all results from memory search
        results = await self._direct_search(query, top_k * 2)
        if not results:
            return []

        # Score by keyword overlap
        query_terms = set(query.lower().split())
        scored = []

        for r in results:
            content = str(r.get("content", r.get("text", ""))).lower()
            score = sum(1 for t in query_terms if t in content)
            if score > 0:
                scored.append((score, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:top_k]]

    async def _direct_search(
        self, query: str, top_k: int,
    ) -> List[Dict[str, Any]]:
        """Direct semantic search using memory."""
        if not self._memory:
            return []

        try:
            results = await self._memory.search(query, top_k=top_k)
            return results if isinstance(results, list) else []
        except Exception as exc:
            logger.warning("advanced_rag direct search failed: %s", exc)
            return []

    # --------------------------------------------------- full pipeline

    async def full_retrieval(
        self,
        query: str,
        top_k: int = 5,
        use_hyde: bool = True,
        use_rerank: bool = True,
        use_hybrid: bool = False,
        use_expansion: bool = False,
        model: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Full retrieval pipeline with all enhancements.

        Pipeline:
        1. (Optional) Query expansion → multiple queries
        2. (Optional) HyDE → generate hypothetical answer
        3. (Optional) Hybrid search → keyword + semantic
        4. (Optional) Rerank → reorder by relevance
        """
        queries = [query]
        if use_expansion:
            queries = await self.expand_query(query, model)

        all_results = []
        for q in queries:
            if use_hyde:
                results = await self.hyde_search(q, top_k * 2, model)
            elif use_hybrid:
                results = await self.hybrid_search(q, top_k * 2)
            else:
                results = await self._direct_search(q, top_k * 2)
            all_results.extend(results)

        # Deduplicate
        seen = set()
        deduped = []
        for r in all_results:
            rid = id(r)
            if rid not in seen:
                seen.add(rid)
                deduped.append(r)

        if use_rerank and len(deduped) > top_k:
            deduped = await self.rerank(query, deduped, top_k, model)
        else:
            deduped = deduped[:top_k]

        return deduped


# Singleton
_advanced_rag: Optional[AdvancedRAG] = None


def get_advanced_rag(llm=None, memory=None) -> AdvancedRAG:
    global _advanced_rag
    if _advanced_rag is None and (llm or memory):
        _advanced_rag = AdvancedRAG(llm, memory)
    elif _advanced_rag is None:
        _advanced_rag = AdvancedRAG(None, None)
    return _advanced_rag