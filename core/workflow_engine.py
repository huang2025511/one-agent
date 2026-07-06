"""Declarative Workflow Engine — define and execute multi-step workflows.

Provides:
  - Workflow definition: declarative YAML/JSON workflow specs
  - Step types: llm_call, tool_call, condition, loop, parallel, wait
  - Variable passing between steps
  - Error handling per step (continue_on_error, fallback)
  - Workflow execution history and status tracking
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class StepType(Enum):
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    CONDITION = "condition"
    LOOP = "loop"
    PARALLEL = "parallel"
    WAIT = "wait"
    SUB_WORKFLOW = "sub_workflow"


@dataclass
class StepResult:
    """Result of a single workflow step."""
    step_id: str = ""
    step_type: StepType = StepType.LLM_CALL
    status: StepStatus = StepStatus.PENDING
    input_data: Dict[str, Any] = field(default_factory=dict)
    output_data: Any = None
    error: str = ""
    duration_ms: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0


@dataclass
class WorkflowResult:
    """Result of a complete workflow execution."""
    workflow_id: str = ""
    workflow_name: str = ""
    steps: List[StepResult] = field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    final_output: Any = None
    total_duration_ms: float = 0.0
    variables: Dict[str, Any] = field(default_factory=dict)


class WorkflowEngine:
    """Declarative workflow execution engine.

    Executes workflows defined in JSON/YAML format. Each workflow
    consists of steps that can call LLMs, execute tools, branch on
    conditions, loop, and run in parallel.

    Example workflow definition:
    {
        "name": "research_and_summarize",
        "steps": [
            {
                "id": "search",
                "type": "tool_call",
                "tool": "web_search",
                "input": {"query": "{{variables.topic}}"}
            },
            {
                "id": "summarize",
                "type": "llm_call",
                "prompt": "Summarize: {{steps.search.output}}",
                "model": "auto"
            }
        ]
    }
    """

    def __init__(self, llm_provider=None, skills_manager=None):
        self._llm = llm_provider
        self._skills = skills_manager
        self._running_workflows: Dict[str, WorkflowResult] = {}

    # --------------------------------------------------- execute

    async def execute(
        self,
        workflow: Dict[str, Any],
        variables: Optional[Dict[str, Any]] = None,
        on_step=None,
        workflow_id: str = "",
    ) -> WorkflowResult:
        """Execute a declarative workflow.

        Args:
            workflow: workflow definition dict
            variables: initial variables
            on_step: callback(step_id, status, output) for progress
            workflow_id: optional workflow ID for tracking
        """
        import uuid

        if not workflow_id:
            workflow_id = uuid.uuid4().hex[:12]

        name = workflow.get("name", "unnamed")
        steps = workflow.get("steps", [])

        result = WorkflowResult(
            workflow_id=workflow_id,
            workflow_name=name,
            variables=variables or {},
        )
        self._running_workflows[workflow_id] = result

        start = time.time()

        for step_def in steps:
            step_result = await self._execute_step(step_def, result.variables)
            result.steps.append(step_result)

            if step_result.output_data is not None:
                result.variables[f"steps.{step_result.step_id}.output"] = step_result.output_data

            if on_step:
                try:
                    on_step(step_result.step_id, step_result.status.value, step_result.output_data)
                except Exception:
                    pass

            # Stop on failure unless continue_on_error is set
            if step_result.status == StepStatus.FAILED:
                if not step_def.get("continue_on_error", False):
                    result.status = StepStatus.FAILED
                    result.total_duration_ms = (time.time() - start) * 1000
                    return result

        result.status = StepStatus.DONE
        result.final_output = result.steps[-1].output_data if result.steps else None
        result.total_duration_ms = (time.time() - start) * 1000

        return result

    async def _execute_step(
        self, step_def: Dict[str, Any], variables: Dict[str, Any],
    ) -> StepResult:
        """Execute a single workflow step."""
        step_id = step_def.get("id", "unknown")
        step_type = step_def.get("type", "llm_call")

        result = StepResult(
            step_id=step_id,
            step_type=StepType(step_type) if step_type in [s.value for s in StepType] else StepType.LLM_CALL,
            input_data=step_def,
            status=StepStatus.RUNNING,
            started_at=time.time(),
        )

        try:
            if step_type == "llm_call":
                output = await self._exec_llm_call(step_def, variables)
                result.output_data = output
                result.status = StepStatus.DONE

            elif step_type == "tool_call":
                output = await self._exec_tool_call(step_def, variables)
                result.output_data = output
                result.status = StepStatus.DONE

            elif step_type == "condition":
                output = await self._exec_condition(step_def, variables)
                result.output_data = output
                result.status = StepStatus.DONE

            elif step_type == "loop":
                output = await self._exec_loop(step_def, variables)
                result.output_data = output
                result.status = StepStatus.DONE

            elif step_type == "parallel":
                output = await self._exec_parallel(step_def, variables)
                result.output_data = output
                result.status = StepStatus.DONE

            elif step_type == "wait":
                delay = float(step_def.get("seconds", 1))
                await asyncio.sleep(delay)
                result.output_data = {"waited": delay}
                result.status = StepStatus.DONE

            else:
                result.status = StepStatus.SKIPPED
                result.output_data = None

        except Exception as exc:
            result.error = str(exc)
            result.status = StepStatus.FAILED
            logger.warning("workflow step '%s' failed: %s", step_id, exc)

        result.completed_at = time.time()
        result.duration_ms = (result.completed_at - result.started_at) * 1000
        return result

    # --------------------------------------------------- step executors

    async def _exec_llm_call(
        self, step_def: Dict[str, Any], variables: Dict[str, Any],
    ) -> str:
        """Execute an LLM call step."""
        if not self._llm:
            return "[LLM not available]"

        prompt = self._resolve_template(step_def.get("prompt", ""), variables)
        system_prompt = self._resolve_template(step_def.get("system_prompt", ""), variables)
        model = step_def.get("model")
        temperature = float(step_def.get("temperature", 0.5))

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        resp = await self._llm.chat_completion(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=int(step_def.get("max_tokens", 1000)),
            tools=None,
        )
        return (resp.get("text") or "").strip()

    async def _exec_tool_call(
        self, step_def: Dict[str, Any], variables: Dict[str, Any],
    ) -> Any:
        """Execute a tool call step."""
        if not self._skills:
            return "[Skills not available]"

        tool_name = step_def.get("tool", "")
        tool_input = step_def.get("input", {})
        tool_input = self._resolve_template_dict(tool_input, variables)

        skill = self._skills.get(tool_name)
        if skill is None:
            return f"[Tool '{tool_name}' not found]"

        if hasattr(skill, "run"):
            return await skill.run(tool_input)
        elif callable(skill):
            return await skill(tool_input)
        return str(skill)

    async def _exec_condition(
        self, step_def: Dict[str, Any], variables: Dict[str, Any],
    ) -> bool:
        """Execute a condition step."""
        condition = step_def.get("condition", "")
        resolved = self._resolve_template(condition, variables)

        # Simple boolean check
        if resolved.lower() in ("true", "yes", "1"):
            return True
        if resolved.lower() in ("false", "no", "0", ""):
            return False

        # Equality check: "a == b"
        import re
        m = re.match(r"(.+?)\s*(==|!=|>=|<=|>|<)\s*(.+)", resolved)
        if m:
            left, op, right = m.group(1).strip(), m.group(2), m.group(3).strip()
            try:
                left_val = float(left) if left.replace(".", "").isdigit() else left
                right_val = float(right) if right.replace(".", "").isdigit() else right
            except ValueError:
                left_val, right_val = left, right

            if op == "==":
                return left_val == right_val
            elif op == "!=":
                return left_val != right_val
            elif op == ">=":
                return left_val >= right_val
            elif op == "<=":
                return left_val <= right_val
            elif op == ">":
                return left_val > right_val
            elif op == "<":
                return left_val < right_val

        return bool(resolved.strip())

    async def _exec_loop(
        self, step_def: Dict[str, Any], variables: Dict[str, Any],
    ) -> List[Any]:
        """Execute a loop step."""
        items = step_def.get("items", [])
        sub_step = step_def.get("step", {})
        max_iterations = int(step_def.get("max_iterations", 10))

        if not sub_step:
            return []

        # Resolve items from variable
        if isinstance(items, str):
            items = self._resolve_template(items, variables)
            if isinstance(items, str):
                items = items.split("\n")

        results = []
        for i, item in enumerate(items[:max_iterations]):
            loop_vars = dict(variables)
            loop_vars["loop.index"] = i
            loop_vars["loop.item"] = item
            step_result = await self._execute_step(sub_step, loop_vars)
            results.append(step_result.output_data)

        return results

    async def _exec_parallel(
        self, step_def: Dict[str, Any], variables: Dict[str, Any],
    ) -> List[Any]:
        """Execute parallel steps."""
        sub_steps = step_def.get("steps", [])
        if not sub_steps:
            return []

        tasks = [
            self._execute_step(sub_step, variables)
            for sub_step in sub_steps
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [
            r.output_data if isinstance(r, StepResult) else str(r)
            for r in results
        ]

    # --------------------------------------------------- template resolution

    def _resolve_template(self, text: str, variables: Dict[str, Any]) -> str:
        """Resolve {{variable}} templates in a string."""
        if not text or "{{" not in text:
            return text

        import re

        def replace_var(match):
            var_path = match.group(1).strip()
            return str(self._resolve_var(var_path, variables))

        return re.sub(r"\{\{(.+?)\}\}", replace_var, text)

    def _resolve_template_dict(
        self, d: Dict[str, Any], variables: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Resolve templates in all values of a dict."""
        result = {}
        for k, v in d.items():
            if isinstance(v, str):
                result[k] = self._resolve_template(v, variables)
            elif isinstance(v, dict):
                result[k] = self._resolve_template_dict(v, variables)
            else:
                result[k] = v
        return result

    def _resolve_var(self, path: str, variables: Dict[str, Any]) -> Any:
        """Resolve a variable path like 'steps.search.output'."""
        parts = path.split(".")
        current = variables
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, str):
                try:
                    current = json.loads(current)
                    current = current.get(part)
                except (json.JSONDecodeError, AttributeError):
                    return ""
            else:
                return ""
        return current if current is not None else ""

    # --------------------------------------------------- management

    def get_workflow_status(self, workflow_id: str) -> Optional[WorkflowResult]:
        return self._running_workflows.get(workflow_id)

    def cancel_workflow(self, workflow_id: str) -> bool:
        if workflow_id in self._running_workflows:
            self._running_workflows[workflow_id].status = StepStatus.FAILED
            return True
        return False

    def list_running(self) -> List[Dict[str, Any]]:
        return [
            {"id": wid, "name": r.workflow_name, "status": r.status.value}
            for wid, r in self._running_workflows.items()
        ]


# Singleton
_workflow_engine: Optional[WorkflowEngine] = None


def get_workflow_engine(llm=None, skills=None) -> WorkflowEngine:
    global _workflow_engine
    if _workflow_engine is None and (llm or skills):
        _workflow_engine = WorkflowEngine(llm, skills)
    elif _workflow_engine is None:
        _workflow_engine = WorkflowEngine(None, None)
    return _workflow_engine