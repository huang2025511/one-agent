"""Multi-Agent Mesh — specialized agent collaboration network.

Provides:
  - AgentMesh: coordinator for multiple specialized agents
  - Specialized agents: researcher, coder, reviewer, writer, analyst
  - Task routing: auto-route tasks to the right specialized agent
  - Agent-to-agent handoff: agents can delegate to each other
  - Shared context: agents share a common knowledge base
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class AgentRole(Enum):
    """Specialized agent roles."""
    RESEARCHER = "researcher"   # search, gather information
    CODER = "coder"             # write/analyze code
    REVIEWER = "reviewer"       # review and critique
    WRITER = "writer"           # generate content
    ANALYST = "analyst"         # data analysis
    PLANNER = "planner"         # task decomposition
    EXECUTOR = "executor"       # execute actions
    COORDINATOR = "coordinator" # orchestrate agents


@dataclass
class AgentTask:
    """A task assigned to a specialized agent."""
    task_id: str = ""
    role: AgentRole = AgentRole.RESEARCHER
    instruction: str = ""
    context: str = ""
    result: str = ""
    status: str = "pending"  # pending, running, done, failed
    started_at: float = 0.0
    completed_at: float = 0.0
    parent_task_id: str = ""


@dataclass
class MeshResult:
    """Result of a multi-agent collaboration."""
    original_task: str = ""
    tasks: List[AgentTask] = field(default_factory=list)
    final_answer: str = ""
    total_duration: float = 0.0
    agent_count: int = 0


class AgentMesh:
    """Multi-agent collaboration mesh.

    Coordinates specialized agents to solve complex tasks that require
    multiple expertise domains. Agents can delegate sub-tasks to each other.

    Usage:
        mesh = AgentMesh(llm, skills)
        result = await mesh.solve("Write a research report on quantum computing")
    """

    def __init__(self, llm_provider=None, skills_manager=None):
        self._llm = llm_provider
        self._skills = skills_manager
        self._role_system_prompts: Dict[AgentRole, str] = self._build_role_prompts()

    def _build_role_prompts(self) -> Dict[AgentRole, str]:
        """Build specialized system prompts for each agent role."""
        return {
            AgentRole.RESEARCHER: (
                "你是一个专业的研究员。你的任务是搜索、收集和分析信息。"
                "对于每个研究任务，你应该：\n"
                "1. 使用 web_search 搜索相关信息\n"
                "2. 从搜索结果中提取关键信息\n"
                "3. 整理成结构化的研究报告\n"
                "只输出研究结果，不要做其他事情。"
            ),
            AgentRole.CODER: (
                "你是一个专业的程序员。你的任务是编写、分析和审查代码。"
                "对于每个编程任务，你应该：\n"
                "1. 理解需求\n"
                "2. 编写清晰、高效的代码\n"
                "3. 添加必要的注释\n"
                "只输出代码，不要做其他事情。"
            ),
            AgentRole.REVIEWER: (
                "你是一个严格的审查员。你的任务是审查和批判性分析。"
                "对于每个审查任务，你应该：\n"
                "1. 找出问题、漏洞、不一致之处\n"
                "2. 提出改进建议\n"
                "3. 给出明确的通过/不通过意见\n"
                "只输出审查意见，不要做其他事情。"
            ),
            AgentRole.WRITER: (
                "你是一个专业的写作者。你的任务是生成高质量的文字内容。"
                "对于每个写作任务，你应该：\n"
                "1. 组织清晰的结构\n"
                "2. 使用流畅、专业的语言\n"
                "3. 确保内容准确、有说服力\n"
                "只输出写作内容，不要做其他事情。"
            ),
            AgentRole.ANALYST: (
                "你是一个数据分析师。你的任务是分析数据并提取洞察。"
                "对于每个分析任务，你应该：\n"
                "1. 理解数据结构和含义\n"
                "2. 找出模式、趋势和异常\n"
                "3. 给出可操作的结论\n"
                "只输出分析结果，不要做其他事情。"
            ),
            AgentRole.PLANNER: (
                "你是一个任务规划专家。你的任务是将复杂任务分解为可执行的步骤。"
                "对于每个规划任务，你应该：\n"
                "1. 分析任务的组成部分\n"
                "2. 确定执行顺序和依赖关系\n"
                "3. 为每个子任务指派合适的角色\n"
                "输出格式：每行一个子任务，格式为 '角色: 任务描述'"
            ),
        }

    # --------------------------------------------------- solve

    async def solve(
        self,
        task: str,
        model: Optional[str] = None,
        max_agents: int = 5,
        on_progress=None,
    ) -> MeshResult:
        """Solve a complex task using multiple specialized agents.

        Workflow:
        1. Planner decomposes the task
        2. Each sub-task is routed to the appropriate specialized agent
        3. Results are collected and synthesized
        """
        start = time.time()

        # Phase 1: Plan
        if on_progress:
            on_progress("planning", "正在规划任务分解...")
        plan = await self._plan(task, model)

        if not plan:
            # Simple task: execute directly
            answer = await self._execute_agent(
                AgentRole.RESEARCHER, task, "", model,
            )
            return MeshResult(
                original_task=task,
                tasks=[AgentTask(
                    task_id="direct",
                    role=AgentRole.RESEARCHER,
                    instruction=task,
                    result=answer,
                )],
                final_answer=answer,
                total_duration=time.time() - start,
                agent_count=1,
            )

        # Phase 2: Execute sub-tasks
        tasks: List[AgentTask] = []
        for i, (role, instruction) in enumerate(plan[:max_agents]):
            if on_progress:
                on_progress("executing", f"执行中 ({i+1}/{len(plan[:max_agents])}): {role.value}")

            task_id = f"task_{i}"
            agent_task = AgentTask(
                task_id=task_id,
                role=role,
                instruction=instruction,
                status="running",
                started_at=time.time(),
            )
            tasks.append(agent_task)

            result = await self._execute_agent(role, instruction, task, model)
            agent_task.result = result
            agent_task.status = "done"
            agent_task.completed_at = time.time()

        # Phase 3: Synthesize
        if on_progress:
            on_progress("synthesizing", "正在综合结果...")
        final_answer = await self._synthesize(task, tasks, model)

        return MeshResult(
            original_task=task,
            tasks=tasks,
            final_answer=final_answer,
            total_duration=time.time() - start,
            agent_count=len(tasks),
        )

    async def _plan(
        self, task: str, model: Optional[str],
    ) -> List[tuple]:
        """Decompose task into (role, instruction) pairs."""
        planner_prompt = self._role_system_prompts[AgentRole.PLANNER]

        try:
            resp = await self._llm.chat_completion(
                messages=[
                    {"role": "system", "content": planner_prompt},
                    {"role": "user", "content": f"分解以下任务：\n{task}"},
                ],
                model=model,
                temperature=0.3,
                max_tokens=500,
                tools=None,
            )
            plan_text = (resp.get("text") or "").strip()
        except Exception as exc:
            logger.warning("agent_mesh plan failed: %s", exc)
            return []

        return self._parse_plan(plan_text)

    def _parse_plan(self, text: str) -> List[tuple]:
        """Parse plan text into (role, instruction) pairs."""
        import re

        role_map = {
            "研究员": AgentRole.RESEARCHER, "研究者": AgentRole.RESEARCHER,
            "researcher": AgentRole.RESEARCHER,
            "程序员": AgentRole.CODER, "开发者": AgentRole.CODER,
            "coder": AgentRole.CODER, "developer": AgentRole.CODER,
            "审查员": AgentRole.REVIEWER, "审核员": AgentRole.REVIEWER,
            "reviewer": AgentRole.REVIEWER,
            "写作者": AgentRole.WRITER, "作者": AgentRole.WRITER,
            "writer": AgentRole.WRITER,
            "分析师": AgentRole.ANALYST, "数据分析": AgentRole.ANALYST,
            "analyst": AgentRole.ANALYST,
        }

        plan = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue

            # Try "角色: 任务" format
            m = re.match(r"([^:：]+)[：:]\s*(.+)", line)
            if m:
                role_name = m.group(1).strip()
                instruction = m.group(2).strip()
                role = role_map.get(role_name, AgentRole.RESEARCHER)
                plan.append((role, instruction))
                continue

            # Try numbered list
            m = re.match(r"\d+[\.\)、]\s*([^:：]+)[：:]\s*(.+)", line)
            if m:
                role_name = m.group(1).strip()
                instruction = m.group(2).strip()
                role = role_map.get(role_name, AgentRole.RESEARCHER)
                plan.append((role, instruction))
                continue

        return plan

    async def _execute_agent(
        self, role: AgentRole, instruction: str, context: str, model: Optional[str],
    ) -> str:
        """Execute a task with a specialized agent."""
        system_prompt = self._role_system_prompts.get(
            role, self._role_system_prompts[AgentRole.RESEARCHER],
        )

        messages = [{"role": "system", "content": system_prompt}]
        if context:
            messages.append({"role": "user", "content": f"背景：{context[:500]}"})
        messages.append({"role": "user", "content": instruction})

        try:
            resp = await self._llm.chat_completion(
                messages=messages,
                model=model,
                temperature=0.5,
                max_tokens=1000,
                tools=None,
            )
            return (resp.get("text") or "").strip()
        except Exception as exc:
            return f"[{role.value} 执行失败: {exc}]"

    async def _synthesize(
        self, task: str, tasks: List[AgentTask], model: Optional[str],
    ) -> str:
        """Synthesize results from all agents into a final answer."""
        results_text = ""
        for t in tasks:
            results_text += f"\n### {t.role.value} 结果\n{t.result[:500]}\n"

        prompt = (
            "请将以下多个专家的分析结果整合成一个连贯的最终回答。\n\n"
            f"原始任务：{task}\n\n"
            f"专家结果：{results_text[:4000]}\n\n"
            "最终综合回答："
        )

        try:
            resp = await self._llm.chat_completion(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                temperature=0.5,
                max_tokens=1500,
                tools=None,
            )
            return (resp.get("text") or "").strip()
        except Exception as exc:
            return f"综合失败: {exc}"

    # --------------------------------------------------- single agent (for direct use)

    async def run_agent(
        self, role: AgentRole, instruction: str, model: Optional[str] = None,
    ) -> str:
        """Run a single specialized agent directly."""
        return await self._execute_agent(role, instruction, "", model)

    def format_result(self, result: MeshResult) -> str:
        """Format a MeshResult for display."""
        lines = [
            f"多智能体协作完成",
            f"─────────────────",
            f"任务: {result.original_task[:100]}",
            f"Agent 数: {result.agent_count} | 耗时: {result.total_duration:.1f}s",
            "",
        ]
        for t in result.tasks:
            status_icon = "✓" if t.status == "done" else "✗"
            lines.append(f"  {status_icon} {t.role.value}: {t.instruction[:80]}")
        lines.append("")
        lines.append(result.final_answer)
        return "\n".join(lines)


# Singleton
_agent_mesh: Optional[AgentMesh] = None


def get_agent_mesh(llm=None, skills=None) -> AgentMesh:
    global _agent_mesh
    if _agent_mesh is None and (llm or skills):
        _agent_mesh = AgentMesh(llm, skills)
    elif _agent_mesh is None:
        _agent_mesh = AgentMesh(None, None)
    return _agent_mesh