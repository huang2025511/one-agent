"""Cron-style task scheduler — OpenClaw/Hermes proactive behaviour.

Uses APScheduler for the heavy lifting.  Exposes a tiny API for other
plugins to register jobs, plus reads jobs from a YAML file at boot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore
    _HAS_AP = True
except Exception:  # noqa: BLE001
    _HAS_AP = False

from core.plugin import Plugin

logger = logging.getLogger(__name__)


@dataclass
class WorkflowStep:
    """工作流步骤定义。"""
    name: str               # 步骤名称
    action: str             # 动作类型：llm_call / tool_call / delay / condition / notify
    params: Dict[str, Any]  # 动作参数
    condition: str = ""     # 条件表达式（仅 action=condition 时使用）
    on_success: str = ""    # 成功后跳转的步骤名（空=继续下一步）
    on_failure: str = "stop"  # 失败后行为：stop / continue / 跳转步骤名
    timeout: float = 60.0   # 超时秒数


@dataclass
class Workflow:
    """工作流定义。"""
    id: str
    name: str
    description: str = ""
    steps: List[WorkflowStep] = field(default_factory=list)
    trigger: str = "manual"  # manual / cron
    cron: str = ""           # trigger=cron 时的 cron 表达式
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    last_run: float = 0.0
    last_status: str = ""    # success / failed / running


class SchedulerPlugin(Plugin):
    """Proactive task driver.

    Without any scheduler the agent is strictly reactive — it only responds
    to user messages.  This plugin fires "cron" events at configured
    intervals so other plugins can do work without a user message.
    """

    name = "scheduler"

    def __init__(self) -> None:
        super().__init__()
        self._scheduler: Optional["AsyncIOScheduler"] = None
        self._enabled = True
        self._jobs_file: Optional[str] = None
        self._workflows: Dict[str, Workflow] = {}
        self._workflows_file: Optional[str] = None

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        cfg = ctx.config.get("scheduler") or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._jobs_file = cfg.get("user_jobs_file")
        self._workflows_file = cfg.get("workflows_file")
        # 加载工作流配置
        for wf_cfg in cfg.get("workflows", []) or []:
            try:
                workflow = self._workflow_from_dict(wf_cfg)
                if workflow is None:
                    continue
                self._workflows[workflow.id] = workflow
                # 如果是 cron 触发且有 cron 表达式，注册定时任务
                if workflow.trigger == "cron" and workflow.cron and workflow.enabled:
                    self._register_workflow_cron(workflow)
                logger.info("loaded workflow: %s (%s)", workflow.id, workflow.name)
            except Exception as exc:  # noqa: BLE001
                logger.error("failed to load workflow config: %s", exc, exc_info=True)
        if not self._enabled or not _HAS_AP:
            logger.info("scheduler disabled (has_apscheduler=%s)", _HAS_AP)
            return
        self._scheduler = AsyncIOScheduler(
            timezone=cfg.get("timezone") or ctx.config.get("agent", {}).get("timezone")
        )
        for job in cfg.get("builtin_jobs", []) or []:
            if not job.get("enabled", True):
                continue
            self._register_cron_event(job["name"], job.get("cron", "0 * * * *"))

    async def start(self) -> None:
        if self._scheduler is not None:
            self._scheduler.start()
            logger.info("scheduler started (%d jobs)", len(self._scheduler.get_jobs()))

    async def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
        await super().stop()

    # ------------------------------------------------------------ public
    def add_cron(self, cron: str, func: Callable[..., Any], name: str) -> None:
        if self._scheduler is None:
            return
        parts = cron.split()
        if len(parts) != 5:
            logger.error("invalid cron expression '%s': expected 5 fields, got %d", cron, len(parts))
            return
        minute, hour, day, month, day_of_week = parts
        self._scheduler.add_job(
            func, "cron",
            minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week,
            id=name, replace_existing=True,
        )

    # ------------------------------------------------------------ internal
    def _register_cron_event(self, name: str, cron: str) -> None:
        def fire():
            if self.bus is not None:
                self.bus.publish({  # type: ignore[attr-defined]
                    "type": "cron",
                    "payload": {"name": name},
                })
            logger.debug("cron fired: %s", name)
        self.add_cron(cron, fire, name)

    # ------------------------------------------------------------ workflows
    def _workflow_from_dict(self, data: Dict[str, Any]) -> Optional[Workflow]:
        """将配置字典转换为 Workflow 对象。"""
        if not data or not data.get("id") or not data.get("name"):
            return None
        steps: List[WorkflowStep] = []
        for step_cfg in data.get("steps", []) or []:
            steps.append(WorkflowStep(
                name=step_cfg.get("name", ""),
                action=step_cfg.get("action", ""),
                params=step_cfg.get("params", {}) or {},
                condition=step_cfg.get("condition", ""),
                on_success=step_cfg.get("on_success", ""),
                on_failure=step_cfg.get("on_failure", "stop"),
                timeout=float(step_cfg.get("timeout", 60.0)),
            ))
        return Workflow(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            steps=steps,
            trigger=data.get("trigger", "manual"),
            cron=data.get("cron", ""),
            enabled=bool(data.get("enabled", True)),
        )

    def _workflow_to_dict(self, workflow: Workflow) -> Dict[str, Any]:
        """将 Workflow 对象转换为可序列化字典。"""
        return {
            "id": workflow.id,
            "name": workflow.name,
            "description": workflow.description,
            "steps": [
                {
                    "name": s.name,
                    "action": s.action,
                    "params": s.params,
                    "condition": s.condition,
                    "on_success": s.on_success,
                    "on_failure": s.on_failure,
                    "timeout": s.timeout,
                }
                for s in workflow.steps
            ],
            "trigger": workflow.trigger,
            "cron": workflow.cron,
            "enabled": workflow.enabled,
            "created_at": workflow.created_at,
            "last_run": workflow.last_run,
            "last_status": workflow.last_status,
        }

    def _register_workflow_cron(self, workflow: Workflow) -> None:
        """为 cron 触发的工作流注册定时任务。"""
        wf_id = workflow.id

        def fire():
            # 定时触发时通过事件循环执行工作流
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            if loop.is_running():
                # 已有运行中的循环，创建任务即可
                asyncio.ensure_future(self.run_workflow(wf_id))
            else:
                loop.run_until_complete(self.run_workflow(wf_id))

        self.add_cron(workflow.cron, fire, f"workflow_{wf_id}")

    def add_workflow(self, workflow: Workflow) -> None:
        """添加或更新工作流。如果是 cron 触发，自动注册定时任务。"""
        self._workflows[workflow.id] = workflow
        # 如果是 cron 触发且有 cron 表达式，注册定时任务
        if workflow.trigger == "cron" and workflow.cron and workflow.enabled:
            self._register_workflow_cron(workflow)
        logger.info("workflow added: %s (%s)", workflow.id, workflow.name)

    def remove_workflow(self, workflow_id: str) -> bool:
        """移除工作流。"""
        if workflow_id not in self._workflows:
            return False
        workflow = self._workflows.pop(workflow_id)
        # 如果是 cron 触发，移除对应的定时任务
        if workflow.trigger == "cron" and self._scheduler is not None:
            try:
                self._scheduler.remove_job(f"workflow_{workflow_id}")
            except Exception:  # noqa: BLE001
                pass
        logger.info("workflow removed: %s", workflow_id)
        return True

    def list_workflows(self) -> List[Dict[str, Any]]:
        """列出所有工作流。"""
        return [self._workflow_to_dict(w) for w in self._workflows.values()]

    def get_workflow(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """获取单个工作流详情。"""
        workflow = self._workflows.get(workflow_id)
        if workflow is None:
            return None
        return self._workflow_to_dict(workflow)

    async def _execute_step(
        self,
        step: WorkflowStep,
        context: Dict[str, Any],
    ) -> Any:
        """执行单个工作流步骤，返回步骤结果。"""
        action = step.action
        params = step.params or {}

        if action == "llm_call":
            # 调用 LLM
            llm_provider = context.get("llm_provider")
            if llm_provider is None:
                raise RuntimeError("llm_call 需要 context 中提供 llm_provider")
            return await llm_provider.chat_completion(**params)

        if action == "tool_call":
            # 调用工具
            tools_registry = context.get("tools_registry")
            if tools_registry is None:
                raise RuntimeError("tool_call 需要 context 中提供 tools_registry")
            tool_name = params.get("tool_name")
            args = params.get("args", {})
            if not tool_name:
                raise RuntimeError("tool_call 需要 params 中提供 tool_name")
            tool = None
            # 兼容 dict 与对象两种 registry 形式
            if isinstance(tools_registry, dict):
                tool = tools_registry.get(tool_name)
            else:
                tool = getattr(tools_registry, tool_name, None)
            if tool is None:
                raise RuntimeError(f"未找到工具: {tool_name}")
            if asyncio.iscoroutinefunction(tool):
                return await tool(**args)
            return tool(**args)

        if action == "delay":
            # 延时
            seconds = float(params.get("seconds", 1))
            await asyncio.sleep(seconds)
            return {"delayed": seconds}

        if action == "condition":
            # 条件判断（安全 eval）
            expression = params.get("expression", "True")
            result = bool(eval(expression, {"__builtins__": {}}, context))  # noqa: S307
            return result

        if action == "notify":
            # 发送通知
            if self.bus is not None:
                self.bus.publish({  # type: ignore[attr-defined]
                    "type": "workflow_notify",
                    "payload": params,
                })
            return {"notified": params}

        raise RuntimeError(f"未知的工作流动作类型: {action}")

    async def run_workflow(
        self,
        workflow_id: str,
        context: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """执行工作流。返回执行结果。"""
        workflow = self._workflows.get(workflow_id)
        if workflow is None:
            return {
                "workflow_id": workflow_id,
                "status": "failed",
                "steps_executed": [],
                "error": f"工作流不存在: {workflow_id}",
            }

        if context is None:
            context = {}

        # 标记为运行中
        workflow.last_run = time.time()
        workflow.last_status = "running"

        steps_executed: List[Dict[str, Any]] = []
        # 构建步骤名到索引的映射，便于跳转
        name_to_index: Dict[str, int] = {}
        for idx, step in enumerate(workflow.steps):
            if step.name:
                name_to_index[step.name] = idx

        result: Dict[str, Any] = {
            "workflow_id": workflow_id,
            "status": "success",
            "steps_executed": steps_executed,
            "error": "",
        }

        i = 0
        n = len(workflow.steps)
        try:
            while 0 <= i < n:
                step = workflow.steps[i]
                step_record: Dict[str, Any] = {
                    "name": step.name,
                    "action": step.action,
                    "status": "running",
                    "error": "",
                }
                try:
                    # 用 asyncio.wait_for 包装以支持超时
                    step_result = await asyncio.wait_for(
                        self._execute_step(step, context),
                        timeout=step.timeout,
                    )
                    step_record["status"] = "success"
                    step_record["result"] = step_result
                    steps_executed.append(step_record)

                    # condition 步骤根据结果决定跳转
                    if step.action == "condition":
                        if step_result:
                            next_target = step.on_success
                        else:
                            next_target = step.on_failure if step.on_failure not in ("stop", "continue") else ""
                        if next_target:
                            if next_target not in name_to_index:
                                raise RuntimeError(f"跳转目标步骤不存在: {next_target}")
                            i = name_to_index[next_target]
                            continue
                        # 没有指定跳转，继续下一步
                        i += 1
                        continue

                    # 普通步骤成功后跳转
                    if step.on_success:
                        if step.on_success not in name_to_index:
                            raise RuntimeError(f"跳转目标步骤不存在: {step.on_success}")
                        i = name_to_index[step.on_success]
                    else:
                        i += 1
                except asyncio.TimeoutError:
                    step_record["status"] = "failed"
                    step_record["error"] = f"步骤超时 ({step.timeout}s)"
                    steps_executed.append(step_record)
                    behavior = step.on_failure
                    if behavior == "stop":
                        result["status"] = "failed"
                        result["error"] = step_record["error"]
                        break
                    elif behavior == "continue":
                        i += 1
                        continue
                    else:
                        # 跳转到指定步骤
                        if behavior not in name_to_index:
                            result["status"] = "failed"
                            result["error"] = f"失败跳转目标步骤不存在: {behavior}"
                            break
                        i = name_to_index[behavior]
                        continue
                except Exception as exc:  # noqa: BLE001
                    step_record["status"] = "failed"
                    step_record["error"] = str(exc)
                    steps_executed.append(step_record)
                    behavior = step.on_failure
                    if behavior == "stop":
                        result["status"] = "failed"
                        result["error"] = step_record["error"]
                        break
                    elif behavior == "continue":
                        i += 1
                        continue
                    else:
                        if behavior not in name_to_index:
                            result["status"] = "failed"
                            result["error"] = f"失败跳转目标步骤不存在: {behavior}"
                            break
                        i = name_to_index[behavior]
                        continue
        except Exception as exc:  # noqa: BLE001
            result["status"] = "failed"
            result["error"] = str(exc)

        # 更新工作流状态
        workflow.last_status = result["status"]
        return result
