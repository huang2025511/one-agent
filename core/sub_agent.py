"""Sub-agent delegation — parallel subtask execution."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from core.context import TurnContext

logger = logging.getLogger(__name__)


class SubAgent:
    """A lightweight agent that executes a single subtask."""

    def __init__(self, llm_provider, skills_manager=None, name: str = ""):
        self._llm = llm_provider
        self._skills = skills_manager
        self.name = name or f"sub-{id(self):x}"[:12]

    async def run(self, task: str, model: str = None, max_iterations: int = 2) -> Dict[str, Any]:
        """Execute a single subtask. Returns {result, tokens_used, duration_ms}."""
        start = time.time()
        messages = [
            {"role": "system", "content": (
                "你是子 Agent，专门执行单一子任务。用 1-2 步完成，直接返回结果。"
                "不要问问题，不要解释过程，只输出最终答案。"
            )},
            {"role": "user", "content": task},
        ]

        try:
            resp = await self._llm.chat_completion(
                messages=messages,
                model=model,
                max_tokens=1000,
                tools=None,
            )
            return {
                "result": resp.get("text", ""),
                "tokens_used": resp.get("tokens_used", 0),
                "duration_ms": (time.time() - start) * 1000,
                "error": None,
            }
        except Exception as exc:
            logger.warning("SubAgent %s failed: %s", self.name, exc)
            return {
                "result": f"[{self.name} 执行失败: {exc}]",
                "tokens_used": 0,
                "duration_ms": (time.time() - start) * 1000,
                "error": str(exc),
            }


class DelegationManager:
    """Decompose complex tasks and run subtasks in parallel."""

    def __init__(self, llm_provider, skills_manager=None):
        self._llm = llm_provider
        self._skills = skills_manager
        self._max_parallel = 3

    async def decompose(self, task: str, model: str = None) -> List[str]:
        """Use LLM to decompose a complex task into subtasks."""
        prompt = [
            {"role": "system", "content": (
                "你是任务分解专家。将用户任务分解为 2-4 个独立的子任务，"
                "每个子任务可以独立执行。用简洁的列表返回，每行一个子任务。"
                "只输出子任务列表，不要编号，不要解释。"
            )},
            {"role": "user", "content": f"分解这个任务：{task}"},
        ]
        try:
            resp = await self._llm.chat_completion(
                messages=prompt,
                model=model,
                max_tokens=500,
                tools=None,
            )
            text = resp.get("text", "")
            subtasks = [s.strip() for s in text.split("\n") if s.strip() and len(s.strip()) > 10]
            return subtasks[:self._max_parallel]
        except (asyncio.TimeoutError, httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("DelegationManager decompose failed: %s", exc)
            return [task]

    async def execute(self, task: str, model: str = None) -> Dict[str, Any]:
        """Decompose and execute subtasks in parallel."""
        start = time.time()

        # Step 1: Decompose
        subtasks = await self.decompose(task, model)

        if len(subtasks) <= 1:
            # Simple task — execute directly
            agent = SubAgent(self._llm, self._skills, "solo")
            result = await agent.run(task, model)
            result["subtasks"] = subtasks
            result["parallel"] = False
            return result

        # Step 2: Execute subtasks in parallel
        agents = [SubAgent(self._llm, self._skills, f"sub-{i}") for i in range(len(subtasks))]
        tasks_coros = [agent.run(subtask, model) for agent, subtask in zip(agents, subtasks)]
        results = await asyncio.gather(*tasks_coros, return_exceptions=True)

        # Step 3: Collect and summarize
        all_results = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                all_results.append(f"[子任务 {i} 异常: {r}]")
            else:
                all_results.append(r.get("result", ""))

        # Merge results
        merged = "\n\n".join(
            f"## 子任务 {i+1}: {subtasks[i]}\n{all_results[i]}"
            for i in range(len(subtasks))
        )

        return {
            "result": merged,
            "subtask_results": all_results,
            "subtasks": subtasks,
            "parallel": True,
            "total_tokens": sum(r.get("tokens_used", 0) for r in results if isinstance(r, dict)),
            "duration_ms": (time.time() - start) * 1000,
        }