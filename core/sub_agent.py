"""Sub-agent delegation — parallel subtask execution."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class SubAgent:
    """A lightweight agent that executes a single subtask.

    修复：之前 tools=None + max_iterations=2 → 子 agent 不能调任何工具，
    纯文本补全，无法搜索/执行代码/读写文件。现在如果传了 skills_manager，
    会构建工具表并跑真正的 tool-calling 循环。
    """

    def __init__(self, llm_provider, skills_manager=None, name: str = ""):
        self._llm = llm_provider
        self._skills = skills_manager
        self.name = name or f"sub-{id(self):x}"[:12]

    def _build_tools(self) -> Optional[List[Dict[str, Any]]]:
        """从 skills_manager 构建工具表。无 skills 或模型不支持工具时返回 None。"""
        if self._skills is None:
            return None
        try:
            # 只挑与子任务相关的少量 skill，避免工具表过大
            # 这里简单取全部（子任务通常用 web_search/calc/system_run 等）
            # 使用 visible_skill_ids() 过滤掉 hidden 的已弃用技能
            tool_list = []
            for sid in self._skills.visible_skill_ids():
                skill = self._skills.get(sid)
                if skill is None:
                    continue
                tool_list.append({
                    "type": "function",
                    "function": {
                        "name": skill.id,
                        "description": (skill.description or skill.title)[:200],
                        "parameters": skill.schema.get("parameters", {
                            "type": "object", "properties": {}
                        }),
                    },
                })
            return tool_list[:8] if tool_list else None  # 最多 8 个工具
        except Exception as exc:
            logger.debug("SubAgent build_tools failed: %s", exc)
            return None

    async def run(self, task: str, model: Optional[str] = None, max_iterations: int = 3) -> Dict[str, Any]:
        """Execute a single subtask. Returns {result, tokens_used, duration_ms}.

        如果有 skills_manager，会跑 tool-calling 循环（最多 max_iterations 轮）。
        """
        start = time.time()
        messages = [
            {"role": "system", "content": (
                "你是子 Agent，专门执行单一子任务。用 1-3 步完成，直接返回结果。"
                "如果需要搜索、计算、执行命令，请调用提供的工具。"
                "不要问问题，不要解释过程，只输出最终答案。"
            )},
            {"role": "user", "content": task},
        ]

        tools = self._build_tools()
        total_tokens = 0

        try:
            # Tool-calling 循环（如果有工具且模型支持）
            for iteration in range(max_iterations):
                resp = await self._llm.chat_completion(
                    messages=messages,
                    model=model,
                    max_tokens=1500,
                    tools=tools,
                )
                total_tokens += int(resp.get("tokens_used") or 0)
                text = resp.get("text", "")
                tool_calls = resp.get("tool_calls") or []

                if not tool_calls:
                    # 没有工具调用 → 直接返回文本
                    return {
                        "result": text or "(no reply)",
                        "tokens_used": total_tokens,
                        "duration_ms": (time.time() - start) * 1000,
                        "error": None,
                        "tool_iterations": iteration,
                    }

                # 执行工具调用
                messages.append({"role": "assistant", "content": text, "tool_calls": tool_calls})
                for tc in tool_calls:
                    fn_name = tc.get("function", {}).get("name", "")
                    fn_args_str = tc.get("function", {}).get("arguments", "{}")
                    import json as _json
                    try:
                        fn_args = _json.loads(fn_args_str) if isinstance(fn_args_str, str) else fn_args_str
                    except _json.JSONDecodeError:
                        fn_args = {"input": fn_args_str}
                    try:
                        tool_result = await self._skills.dispatch(fn_name, fn_args)
                    except Exception as exc:
                        tool_result = f"[工具 {fn_name} 执行失败: {exc}]"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", fn_name),
                        "name": fn_name,
                        "content": str(tool_result)[:2000],
                    })

            # 达到 max_iterations，做一次无工具的最终总结
            resp = await self._llm.chat_completion(
                messages=messages,
                model=model,
                max_tokens=1000,
                tools=None,
            )
            total_tokens += int(resp.get("tokens_used") or 0)
            return {
                "result": resp.get("text", "") or "(no reply)",
                "tokens_used": total_tokens,
                "duration_ms": (time.time() - start) * 1000,
                "error": None,
                "tool_iterations": max_iterations,
            }
        except Exception as exc:
            logger.warning("SubAgent %s failed: %s", self.name, exc)
            return {
                "result": f"[{self.name} 执行失败: {exc}]",
                "tokens_used": total_tokens,
                "duration_ms": (time.time() - start) * 1000,
                "error": str(exc),
            }


class DelegationManager:
    """Decompose complex tasks and run subtasks in parallel.

    未启用：当前 coordinator 的多 agent 委派走 ``core/agent_mesh.py``
    的 ``AgentMesh``（基于角色的专家协作），单点子任务则直接用 ``SubAgent``
    （如 search-summarizer）。本类提供了另一种"分解→并行 SubAgent→合成→
    critic 审查"的实现，但无业务调用方。保留供未来需要 critic 审查闭环的
    场景使用；当前 SubAgent + AgentMesh 已满足需求。
    """

    def __init__(self, llm_provider, skills_manager=None):
        self._llm = llm_provider
        self._skills = skills_manager
        self._max_parallel = 3

    async def decompose(self, task: str, model: Optional[str] = None) -> List[str]:
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
            # 防御：LLM 在无 key / 服务不可用时返回 {"text": "no_api_key...",
            # "failed": True}（不抛异常），如果不检查就把错误字符串
            # split 成"子任务"喂给 SubAgent 执行，浪费 token 且毫无意义。
            if resp.get("failed"):
                logger.warning(
                    "DelegationManager decompose: LLM returned failed response: %s",
                    (resp.get("text", "") or "")[:200],
                )
                return [task]
            text = resp.get("text", "")
            subtasks = [s.strip() for s in text.split("\n") if s.strip() and len(s.strip()) > 10]
            return subtasks[:self._max_parallel]
        except (asyncio.TimeoutError, httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("DelegationManager decompose failed: %s", exc)
            return [task]
        except Exception as exc:
            # 兜底：chat_completion 可能抛 RuntimeError / ConnectionError /
            # OSError（cost_tracker 写入失败等），不应让整个 turn 挂掉。
            logger.warning("DelegationManager decompose unexpected error: %s", exc, exc_info=True)
            return [task]

    async def execute(self, task: str, model: Optional[str] = None) -> Dict[str, Any]:
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
        tasks_coros = [agent.run(subtask, model) for agent, subtask in zip(agents, subtasks, strict=True)]
        results = await asyncio.gather(*tasks_coros, return_exceptions=True)

        # Step 3: Collect and summarize
        all_results = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                all_results.append(f"[子任务 {i} 异常: {r}]")
            else:
                all_results.append(r.get("result", ""))

        # Step 4: 用 LLM 合成最终答案（之前只是字符串 join，没有融合步骤）
        merged_raw = "\n\n".join(
            f"## 子任务 {i+1}: {subtasks[i]}\n{all_results[i]}"
            for i in range(len(subtasks))
        )

        try:
            synth_resp = await self._llm.chat_completion(
                messages=[
                    {"role": "system", "content": (
                        "你是合成专家。以下是多个子 Agent 并行执行的结果。"
                        "请把它们融合成一个连贯、完整的最终答案，"
                        "去掉重复内容，保留所有关键信息，按逻辑组织。"
                        "不要提及'子任务'或执行过程，直接给出用户需要的答案。"
                    )},
                    {"role": "user", "content": f"原始任务：{task}\n\n子任务结果：\n{merged_raw}"},
                ],
                model=model,
                max_tokens=2000,
                tools=None,
            )
            final_result = synth_resp.get("text", "") or merged_raw
            synth_tokens = int(synth_resp.get("tokens_used") or 0)
        except Exception as exc:
            logger.warning("DelegationManager synthesize failed: %s", exc)
            final_result = merged_raw  # 合成失败回退到原始拼接
            synth_tokens = 0

        # Step 5 (Critic): 用独立 LLM 调用来审查合成结果
        # Gap 修复：之前只有 planner→executor→synthesizer，没有 critic 审查。
        # 合成器可能放行错误、遗漏或幻觉。Critic 独立评估并给出修正建议。
        critic_tokens = 0
        critic_feedback = ""
        try:
            critic_resp = await self._llm.chat_completion(
                messages=[
                    {"role": "system", "content": (
                        "你是质量审查专家。请审阅以下 AI 生成的答案，"
                        "检查：1) 是否有事实错误 2) 是否遗漏了关键信息 "
                        "3) 是否有逻辑矛盾。如果答案质量良好，回复 PASS。"
                        "如果有问题，用 1-2 句话指出具体问题。"
                    )},
                    {"role": "user", "content": f"原始任务：{task}\n\n答案：{final_result[:3000]}"},
                ],
                model=model,
                max_tokens=200,
                tools=None,
            )
            critic_feedback = (critic_resp.get("text") or "").strip()
            critic_tokens = int(critic_resp.get("tokens_used") or 0)
            if critic_feedback and critic_feedback.upper() != "PASS" and "pass" not in critic_feedback.lower():
                # 将批评意见追加到最终答案末尾
                final_result = (
                    final_result
                    + "\n\n---\n[质量审查] "
                    + critic_feedback
                )
                logger.info("DelegationManager critic flagged issues: %.100s", critic_feedback)
        except Exception as exc:
            logger.debug("DelegationManager critic failed: %s", exc)

        return {
            "result": final_result,
            "subtask_results": all_results,
            "subtasks": subtasks,
            "parallel": True,
            "total_tokens": sum(r.get("tokens_used", 0) for r in results if isinstance(r, dict)) + synth_tokens,
            "duration_ms": (time.time() - start) * 1000,
        }
