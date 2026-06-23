"""多智能体协作引擎模块。

提供 Agent 角色定义、任务分解、消息总线、冲突仲裁、协作编排和群体决策能力，
支持主从架构与对等架构的多 Agent 协作。纯 Python 实现，不依赖外部 AI 服务。

主要组件：
  - AgentRole: Agent 角色数据类
  - Agent: Agent 实例，封装角色与处理函数
  - TaskDecomposer: 任务分解器（串行/并行/条件分支）
  - MessageBus: Agent 间通信总线（发布订阅 + 点对点 + 请求-响应）
  - ConflictResolver: 冲突检测与仲裁（投票/优先级/加权）
  - CollaborationOrchestrator: 协作编排器（主从/对等）
  - GroupDecision: 群体决策（投票/共识/加权）
  - MultiAgentPlugin: 整合以上功能的插件类
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.plugin import Plugin

logger = logging.getLogger(__name__)

__all__ = [
    "AgentRole",
    "Agent",
    "SubTask",
    "TaskGraph",
    "AgentMessage",
    "Conflict",
    "Vote",
    "TaskDecomposer",
    "MessageBus",
    "ConflictResolver",
    "CollaborationOrchestrator",
    "GroupDecision",
    "MultiAgentPlugin",
]


# ------------------------------------------------------------ 数据结构

@dataclass
class AgentRole:
    """Agent 角色定义。"""

    role_id: str          # 角色唯一标识
    name: str             # 角色名称
    description: str      # 角色描述
    system_prompt: str    # 系统提示词
    capabilities: List[str] = field(default_factory=list)  # 能力列表
    tools: List[str] = field(default_factory=list)         # 可用工具列表


@dataclass
class Agent:
    """Agent 实例：封装角色与处理函数。

    handler 为同步或异步函数，签名为 (input: Any) -> Any。
    priority 用于冲突仲裁（数值越大优先级越高）。
    weight 用于群体决策加权。
    """

    agent_id: str
    role: AgentRole
    handler: Callable[..., Any]
    priority: int = 0
    weight: float = 1.0

    async def execute(self, task_input: Any) -> Any:
        """执行任务，返回结果。支持同步与异步 handler。"""
        if asyncio.iscoroutinefunction(self.handler):
            return await self.handler(task_input)
        return self.handler(task_input)


@dataclass
class SubTask:
    """子任务定义。"""

    task_id: str
    description: str
    dependencies: List[str] = field(default_factory=list)  # 依赖的子任务ID
    execution_mode: str = "serial"  # serial / parallel / conditional
    condition: str = ""             # 条件表达式（execution_mode=conditional 时使用）
    assigned_role: str = ""         # 分配的角色ID
    status: str = "pending"         # pending / running / done / failed / skipped
    result: Any = None
    error: str = ""


@dataclass
class TaskGraph:
    """任务图：由若干子任务组成的有向无环图。"""

    root_task: str
    subtasks: List[SubTask] = field(default_factory=list)

    def get(self, task_id: str) -> Optional[SubTask]:
        """按 ID 获取子任务。"""
        for st in self.subtasks:
            if st.task_id == task_id:
                return st
        return None


@dataclass
class AgentMessage:
    """Agent 间消息。"""

    message_id: str
    source: str       # 发送者 agent_id
    target: str       # 接收者 agent_id 或主题
    content: Any
    msg_type: str = "event"  # request / response / broadcast / event
    timestamp: float = field(default_factory=time.time)
    correlation_id: str = ""  # 关联ID，用于请求-响应匹配


@dataclass
class Conflict:
    """冲突记录。"""

    conflict_id: str
    key: str                     # 冲突键标识（如子任务ID）
    outputs: Dict[str, Any]      # agent_id -> output
    resolution: Any = None
    strategy: str = ""


@dataclass
class Vote:
    """投票记录。"""

    voter_id: str
    choice: Any
    weight: float = 1.0


# ------------------------------------------------------------ 任务分解器

class TaskDecomposer:
    """任务分解器：将复杂任务分解为子任务，支持串行/并行/条件分支。

    默认采用启发式策略，依据中文连接词识别串行与并行结构；
    也可注册自定义分解策略或手动构建任务图。
    """

    # 串行连接词（识别顺序步骤）
    _SERIAL_SPLITTERS = [
        "然后", "接着", "之后", "最后", "随后",
        "第一步", "第二步", "第三步", "第四步",
    ]
    # 并行连接词（识别可并行的子任务）
    _PARALLEL_SPLITTERS = ["同时", "并且", "并行", "与此同时", "另外", "此外"]

    def __init__(self) -> None:
        # 自定义分解策略：strategy_name -> callable(task: str) -> List[SubTask]
        self._strategies: Dict[str, Callable[[str], List[SubTask]]] = {}
        self.register_strategy("heuristic", self._heuristic_decompose)

    def register_strategy(self, name: str, strategy: Callable[[str], List[SubTask]]) -> None:
        """注册自定义分解策略。"""
        self._strategies[name] = strategy

    def decompose(self, task: str, strategy: str = "heuristic") -> TaskGraph:
        """分解任务为任务图。"""
        if strategy not in self._strategies:
            logger.warning("未知分解策略 '%s'，回退到 heuristic", strategy)
            strategy = "heuristic"
        subtasks = self._strategies[strategy](task)
        graph = TaskGraph(root_task=task, subtasks=subtasks)
        logger.info("任务分解完成：%d 个子任务（策略=%s）", len(subtasks), strategy)
        return graph

    def add_subtask(
        self,
        graph: TaskGraph,
        description: str,
        dependencies: Optional[List[str]] = None,
        execution_mode: str = "serial",
        condition: str = "",
        assigned_role: str = "",
        task_id: str = "",
    ) -> SubTask:
        """向任务图手动添加子任务（可用于构建条件分支等复杂结构）。"""
        task_id = task_id or f"sub_{len(graph.subtasks)}_{uuid.uuid4().hex[:6]}"
        st = SubTask(
            task_id=task_id,
            description=description,
            dependencies=list(dependencies or []),
            execution_mode=execution_mode,
            condition=condition,
            assigned_role=assigned_role,
        )
        graph.subtasks.append(st)
        return st

    def _heuristic_decompose(self, task: str) -> List[SubTask]:
        """启发式分解：根据连接词识别串行/并行结构。"""
        text = (task or "").strip()
        if not text:
            return []

        # 1. 按串行连接词切分，得到顺序步骤
        serial_segments = self._split_by_keywords(text, self._SERIAL_SPLITTERS)

        subtasks: List[SubTask] = []
        prev_ids: List[str] = []  # 上一组子任务ID（用于建立依赖）

        for seg_idx, segment in enumerate(serial_segments):
            segment = segment.strip()
            if not segment:
                continue
            # 2. 在每个步骤内按并行连接词切分
            parallel_parts = [
                p.strip()
                for p in self._split_by_keywords(segment, self._PARALLEL_SPLITTERS)
                if p.strip()
            ]

            if len(parallel_parts) <= 1:
                # 单一子任务，串行依赖上一组
                task_id = f"sub_{seg_idx}"
                subtasks.append(SubTask(
                    task_id=task_id,
                    description=segment,
                    dependencies=list(prev_ids),
                    execution_mode="serial",
                ))
                prev_ids = [task_id]
            else:
                # 并行子任务组：组内无依赖，组间依赖上一组
                group_ids: List[str] = []
                for p_idx, part in enumerate(parallel_parts):
                    task_id = f"sub_{seg_idx}_{p_idx}"
                    subtasks.append(SubTask(
                        task_id=task_id,
                        description=part,
                        dependencies=list(prev_ids),
                        execution_mode="parallel",
                    ))
                    group_ids.append(task_id)
                prev_ids = group_ids

        # 没有切分出任何子任务时，把整个任务作为单一子任务
        if not subtasks:
            subtasks.append(SubTask(
                task_id="sub_0",
                description=text,
                dependencies=[],
                execution_mode="serial",
            ))
        return subtasks

    @staticmethod
    def _split_by_keywords(text: str, keywords: List[str]) -> List[str]:
        """按关键词切分文本（关键词本身作为分隔符被丢弃）。"""
        # 收集所有关键词出现位置
        positions = []  # list of (index, keyword_length)
        for kw in keywords:
            start = 0
            while True:
                idx = text.find(kw, start)
                if idx == -1:
                    break
                positions.append((idx, len(kw)))
                start = idx + len(kw)
        if not positions:
            return [text]
        positions.sort()
        segments: List[str] = []
        last = 0
        for idx, klen in positions:
            seg = text[last:idx].strip()
            if seg:
                segments.append(seg)
            last = idx + klen
        tail = text[last:].strip()
        if tail:
            segments.append(tail)
        return segments if segments else [text]


# ------------------------------------------------------------ 消息总线

class MessageBus:
    """Agent 通信总线：支持发布订阅模式与点对点通信（含请求-响应）。"""

    def __init__(self) -> None:
        # topic -> 订阅者处理器列表
        self._topic_subscribers: Dict[str, List[Callable[[AgentMessage], Any]]] = {}
        # agent_id -> 直接消息处理器
        self._agent_handlers: Dict[str, Callable[[AgentMessage], Any]] = {}
        # correlation_id -> Future（请求-响应匹配）
        self._pending_requests: Dict[str, asyncio.Future] = {}
        # 消息历史（便于调试与审计）
        self._history: List[AgentMessage] = []

    def register_agent(self, agent_id: str, handler: Callable[[AgentMessage], Any]) -> None:
        """注册 Agent 的直接消息处理器（用于点对点通信）。"""
        self._agent_handlers[agent_id] = handler

    def unregister_agent(self, agent_id: str) -> None:
        """注销 Agent。"""
        self._agent_handlers.pop(agent_id, None)

    def subscribe(self, topic: str, handler: Callable[[AgentMessage], Any]) -> None:
        """订阅主题（发布订阅模式）。"""
        self._topic_subscribers.setdefault(topic, []).append(handler)

    def unsubscribe(self, topic: str, handler: Callable[[AgentMessage], Any]) -> None:
        """取消订阅。"""
        subs = self._topic_subscribers.get(topic)
        if not subs:
            return
        try:
            subs.remove(handler)
        except ValueError:
            pass
        if not subs:
            del self._topic_subscribers[topic]

    async def publish(self, topic: str, content: Any, source: str = "") -> None:
        """发布消息到主题（发布订阅模式，所有订阅者都会收到）。"""
        msg = AgentMessage(
            message_id=uuid.uuid4().hex[:12],
            source=source,
            target=topic,
            content=content,
            msg_type="broadcast",
        )
        self._history.append(msg)
        subscribers = list(self._topic_subscribers.get(topic, []))
        for sub in subscribers:
            try:
                result = sub(msg)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                logger.error("主题 %s 订阅者处理失败: %s", topic, exc, exc_info=True)

    async def send(self, target: str, content: Any, source: str = "") -> bool:
        """点对点发送消息给指定 Agent。返回是否送达。"""
        handler = self._agent_handlers.get(target)
        if handler is None:
            logger.warning("点对点消息未送达：目标 Agent '%s' 未注册", target)
            return False
        msg = AgentMessage(
            message_id=uuid.uuid4().hex[:12],
            source=source,
            target=target,
            content=content,
            msg_type="event",
        )
        self._history.append(msg)
        try:
            result = handler(msg)
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001
            logger.error("点对点消息处理失败 (%s -> %s): %s", source, target, exc, exc_info=True)
            return False
        return True

    async def request(
        self,
        target: str,
        content: Any,
        source: str = "",
        timeout: float = 30.0,
    ) -> Any:
        """请求-响应模式：发送请求并等待响应。"""
        loop = asyncio.get_running_loop()
        correlation_id = uuid.uuid4().hex[:12]
        future: asyncio.Future = loop.create_future()
        self._pending_requests[correlation_id] = future

        handler = self._agent_handlers.get(target)
        if handler is None:
            self._pending_requests.pop(correlation_id, None)
            raise RuntimeError(f"目标 Agent '{target}' 未注册")

        msg = AgentMessage(
            message_id=uuid.uuid4().hex[:12],
            source=source,
            target=target,
            content=content,
            msg_type="request",
            correlation_id=correlation_id,
        )
        self._history.append(msg)

        async def _handle() -> None:
            try:
                result = handler(msg)
                if asyncio.iscoroutine(result):
                    result = await result
                if not future.done():
                    future.set_result(result)
            except Exception as exc:  # noqa: BLE001
                if not future.done():
                    future.set_exception(exc)

        asyncio.create_task(_handle())

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(correlation_id, None)
            raise

    async def response(
        self,
        correlation_id: str,
        content: Any,
        source: str = "",
        target: str = "",
    ) -> None:
        """显式响应一个请求（通常由 Agent 在异步处理完成后调用）。"""
        msg = AgentMessage(
            message_id=uuid.uuid4().hex[:12],
            source=source,
            target=target,
            content=content,
            msg_type="response",
            correlation_id=correlation_id,
        )
        self._history.append(msg)
        future = self._pending_requests.pop(correlation_id, None)
        if future is not None and not future.done():
            future.set_result(content)

    @property
    def history(self) -> List[AgentMessage]:
        """返回消息历史副本。"""
        return list(self._history)


# ------------------------------------------------------------ 冲突检测与仲裁

class ConflictResolver:
    """冲突检测与仲裁：检测 Agent 输出冲突，支持投票/优先级/加权仲裁。"""

    def __init__(self) -> None:
        self._conflicts: List[Conflict] = []

    def detect_conflicts(self, outputs: Dict[str, Any], key: str = "") -> Optional[Conflict]:
        """检测多个 Agent 输出是否存在冲突。

        Args:
            outputs: agent_id -> output 映射
            key: 冲突键标识（如子任务ID）

        Returns:
            冲突对象（存在冲突时），否则 None
        """
        if len(outputs) < 2:
            return None
        values = list(outputs.values())
        first = values[0]
        has_conflict = any(not self._equals(first, v) for v in values[1:])
        if not has_conflict:
            return None
        conflict = Conflict(
            conflict_id=uuid.uuid4().hex[:12],
            key=key or uuid.uuid4().hex[:8],
            outputs=dict(outputs),
        )
        self._conflicts.append(conflict)
        logger.info("检测到冲突 (key=%s, agents=%s)", key, list(outputs.keys()))
        return conflict

    @staticmethod
    def _equals(a: Any, b: Any) -> bool:
        """比较两个输出是否等价。"""
        try:
            return a == b
        except Exception:  # noqa: BLE001
            return False

    def resolve_by_priority(self, conflict: Conflict, agents: Dict[str, Agent]) -> Any:
        """优先级仲裁：选择优先级最高的 Agent 输出。"""
        best_agent: Optional[str] = None
        best_priority = float("-inf")
        for agent_id in conflict.outputs:
            agent = agents.get(agent_id)
            priority = agent.priority if agent else 0
            if priority > best_priority:
                best_priority = priority
                best_agent = agent_id
        if best_agent is None:
            best_agent = next(iter(conflict.outputs))
        resolution = conflict.outputs[best_agent]
        conflict.resolution = resolution
        conflict.strategy = "priority"
        logger.info("冲突仲裁完成（优先级）：采用 %s 的输出", best_agent)
        return resolution

    def resolve_by_voting(self, conflict: Conflict) -> Any:
        """投票仲裁：相同输出计数最多者胜出（多数表决）。"""
        counts: Dict[str, int] = {}
        repr_to_value: Dict[str, Any] = {}
        for output in conflict.outputs.values():
            key = repr(output)
            counts[key] = counts.get(key, 0) + 1
            repr_to_value[key] = output
        best_key = max(counts, key=lambda k: counts[k])
        resolution = repr_to_value[best_key]
        conflict.resolution = resolution
        conflict.strategy = "voting"
        logger.info("冲突仲裁完成（投票）：得票 %d", counts[best_key])
        return resolution

    def resolve_by_weight(self, conflict: Conflict, agents: Dict[str, Agent]) -> Any:
        """加权仲裁：按 Agent 权重加权，权重和最大的输出胜出。"""
        weighted: Dict[str, float] = {}
        repr_to_value: Dict[str, Any] = {}
        for agent_id, output in conflict.outputs.items():
            agent = agents.get(agent_id)
            weight = agent.weight if agent else 1.0
            key = repr(output)
            weighted[key] = weighted.get(key, 0.0) + weight
            repr_to_value[key] = output
        best_key = max(weighted, key=lambda k: weighted[k])
        resolution = repr_to_value[best_key]
        conflict.resolution = resolution
        conflict.strategy = "weight"
        logger.info("冲突仲裁完成（加权）：权重和 %.2f", weighted[best_key])
        return resolution

    @property
    def conflicts(self) -> List[Conflict]:
        """返回已记录的冲突列表副本。"""
        return list(self._conflicts)


# ------------------------------------------------------------ 群体决策

class GroupDecision:
    """群体决策：投票、共识、加权决策。"""

    def __init__(self) -> None:
        self._decisions: List[Dict[str, Any]] = []

    def vote(self, votes: List[Vote], method: str = "majority") -> Any:
        """投票决策。

        Args:
            votes: 投票列表
            method: majority（绝对多数 >50%）/ plurality（相对多数） / weighted（加权）

        Returns:
            获胜选项（majority 未过半数时返回 None）
        """
        if not votes:
            return None
        counts: Dict[str, int] = {}
        weights: Dict[str, float] = {}
        value_map: Dict[str, Any] = {}
        for v in votes:
            key = repr(v.choice)
            counts[key] = counts.get(key, 0) + 1
            weights[key] = weights.get(key, 0.0) + v.weight
            value_map[key] = v.choice

        total = len(votes)
        if method == "weighted":
            best_key = max(weights, key=lambda k: weights[k])
            decision = value_map[best_key]
            detail = {"method": "weighted", "weights": dict(weights), "winner": decision}
        elif method == "plurality":
            best_key = max(counts, key=lambda k: counts[k])
            decision = value_map[best_key]
            detail = {"method": "plurality", "counts": dict(counts), "winner": decision}
        else:  # majority
            best_key = max(counts, key=lambda k: counts[k])
            decision = value_map[best_key] if counts[best_key] > total / 2 else None
            detail = {
                "method": "majority",
                "counts": dict(counts),
                "winner": decision,
                "threshold": total / 2,
            }

        self._decisions.append(detail)
        logger.info("群体决策完成（%s）：获胜=%s", method, decision)
        return decision

    def weighted_decision(self, votes: List[Vote]) -> Any:
        """加权决策便捷方法。"""
        return self.vote(votes, method="weighted")

    async def consensus(
        self,
        voters: List[str],
        propose: Callable[[int, Optional[Any]], Any],
        accept: Callable[[str, Any], bool],
        max_rounds: int = 3,
    ) -> Any:
        """共识决策：迭代提议直到所有投票者接受或达到最大轮数。

        Args:
            voters: 投票者ID列表
            propose: 提议函数 (round_idx, last_proposal) -> new_proposal
            accept: 接受判断函数 (voter_id, proposal) -> bool
            max_rounds: 最大迭代轮数

        Returns:
            达成共识的提议，未达成则 None
        """
        proposal: Any = None
        for round_idx in range(1, max_rounds + 1):
            proposal = propose(round_idx, proposal)
            accepted = all(accept(vid, proposal) for vid in voters)
            if accepted:
                logger.info("共识达成（第 %d 轮）", round_idx)
                self._decisions.append({
                    "method": "consensus",
                    "rounds": round_idx,
                    "proposal": proposal,
                })
                return proposal
            logger.debug("共识未达成（第 %d 轮），继续迭代", round_idx)
        logger.warning("共识未达成，已达最大轮数 %d", max_rounds)
        self._decisions.append({
            "method": "consensus",
            "rounds": max_rounds,
            "proposal": None,
            "failed": True,
        })
        return None

    @property
    def history(self) -> List[Dict[str, Any]]:
        """返回决策历史副本。"""
        return list(self._decisions)


# ------------------------------------------------------------ 协作编排器

class CollaborationOrchestrator:
    """协作编排器：编排多 Agent 协作流程，支持主从架构与对等架构。"""

    def __init__(
        self,
        message_bus: Optional[MessageBus] = None,
        conflict_resolver: Optional[ConflictResolver] = None,
    ) -> None:
        self._agents: Dict[str, Agent] = {}            # agent_id -> Agent
        self._role_agents: Dict[str, List[str]] = {}   # role_id -> [agent_id]
        self._bus = message_bus or MessageBus()
        self._resolver = conflict_resolver or ConflictResolver()

    # -------------------------------------------- 注册与查询
    def register_agent(self, agent: Agent) -> None:
        """注册 Agent，并在消息总线上挂载其直接处理器。"""
        self._agents[agent.agent_id] = agent
        self._role_agents.setdefault(agent.role.role_id, []).append(agent.agent_id)
        self._bus.register_agent(agent.agent_id, self._make_message_handler(agent))
        logger.info("注册 Agent: %s (角色=%s)", agent.agent_id, agent.role.name)

    def unregister_agent(self, agent_id: str) -> None:
        """注销 Agent。"""
        agent = self._agents.pop(agent_id, None)
        if agent is None:
            return
        ids = self._role_agents.get(agent.role.role_id, [])
        if agent_id in ids:
            ids.remove(agent_id)
        self._bus.unregister_agent(agent_id)

    def _make_message_handler(self, agent: Agent) -> Callable[[AgentMessage], Any]:
        """为 Agent 创建消息总线处理器：将消息内容作为任务输入执行。"""
        async def handler(msg: AgentMessage) -> Any:
            return await agent.execute(msg.content)
        return handler

    def get_agent(self, agent_id: str) -> Optional[Agent]:
        return self._agents.get(agent_id)

    def find_agents_by_role(self, role_id: str) -> List[Agent]:
        """按角色ID查找所有匹配的 Agent。"""
        ids = self._role_agents.get(role_id, [])
        return [self._agents[aid] for aid in ids if aid in self._agents]

    @property
    def message_bus(self) -> MessageBus:
        return self._bus

    @property
    def conflict_resolver(self) -> ConflictResolver:
        return self._resolver

    # -------------------------------------------- 主从架构
    async def run_master_slave(self, graph: TaskGraph, master_id: str) -> Dict[str, Any]:
        """主从架构：主 Agent 负责分配与汇总，从 Agent 执行子任务。"""
        master = self._agents.get(master_id)
        if master is None:
            raise RuntimeError(f"主 Agent '{master_id}' 未注册")

        logger.info("启动主从协作：主=%s，子任务数=%d", master_id, len(graph.subtasks))
        results: Dict[str, Any] = {}
        start_time = time.time()

        if not graph.subtasks:
            # 无子任务，主 Agent 直接执行
            try:
                results["__main__"] = await master.execute(graph.root_task)
            except Exception as exc:  # noqa: BLE001
                results["__main__"] = None
                logger.error("主 Agent 执行失败: %s", exc, exc_info=True)
        else:
            await self._execute_graph(graph, results, master_id=master_id, architecture="master_slave")
            # 主 Agent 汇总子任务结果
            try:
                summary = await master.execute({
                    "action": "summarize",
                    "root_task": graph.root_task,
                    "subtask_results": results,
                })
                results["__summary__"] = summary
            except Exception as exc:  # noqa: BLE001
                logger.warning("主 Agent 汇总失败: %s", exc, exc_info=True)

        return {
            "architecture": "master_slave",
            "master": master_id,
            "subtask_results": results,
            "final_result": results.get("__summary__", results.get("__main__")),
            "duration_ms": (time.time() - start_time) * 1000,
        }

    # -------------------------------------------- 对等架构
    async def run_peer_to_peer(self, graph: TaskGraph) -> Dict[str, Any]:
        """对等架构：各 Agent 平等协作，同角色多 Agent 时并发执行并仲裁冲突。"""
        logger.info("启动对等协作：子任务数=%d", len(graph.subtasks))
        results: Dict[str, Any] = {}
        start_time = time.time()

        if not graph.subtasks:
            # 无子任务，任选一个 Agent 执行
            if self._agents:
                agent = next(iter(self._agents.values()))
                try:
                    results["__main__"] = await agent.execute(graph.root_task)
                except Exception as exc:  # noqa: BLE001
                    results["__main__"] = None
                    logger.error("对等 Agent 执行失败: %s", exc, exc_info=True)
            final = results.get("__main__")
        else:
            await self._execute_graph(graph, results, master_id="", architecture="peer_to_peer")
            # 对等架构无指定汇总者，最终结果为各子任务结果集合
            final = results

        return {
            "architecture": "peer_to_peer",
            "subtask_results": results,
            "final_result": final,
            "duration_ms": (time.time() - start_time) * 1000,
            "conflicts": [
                {
                    "key": c.key,
                    "outputs": c.outputs,
                    "resolution": c.resolution,
                    "strategy": c.strategy,
                }
                for c in self._resolver.conflicts
            ],
        }

    # -------------------------------------------- 任务图执行
    async def _execute_graph(
        self,
        graph: TaskGraph,
        results: Dict[str, Any],
        master_id: str = "",
        architecture: str = "master_slave",
    ) -> None:
        """按依赖关系拓扑执行任务图。"""
        pending = list(graph.subtasks)
        while pending:
            ready: List[SubTask] = []
            still_pending: List[SubTask] = []
            for st in pending:
                if all(self._is_dep_satisfied(dep, graph) for dep in st.dependencies):
                    ready.append(st)
                else:
                    still_pending.append(st)

            if not ready:
                # 死锁：剩余子任务依赖无法满足
                stuck = [st.task_id for st in still_pending]
                logger.error("任务图存在无法满足的依赖，卡住的子任务: %s", stuck)
                for st in still_pending:
                    st.status = "failed"
                    st.error = "依赖无法满足"
                break

            # 同一批 ready 中：parallel 模式并发，其余串行
            parallel_tasks = [st for st in ready if st.execution_mode == "parallel"]
            serial_tasks = [st for st in ready if st.execution_mode != "parallel"]

            for st in serial_tasks:
                await self._execute_subtask(st, results, master_id, architecture)

            if parallel_tasks:
                await asyncio.gather(
                    *(
                        self._execute_subtask(st, results, master_id, architecture)
                        for st in parallel_tasks
                    ),
                    return_exceptions=False,
                )

            pending = still_pending

    @staticmethod
    def _is_dep_satisfied(dep_id: str, graph: TaskGraph) -> bool:
        """检查依赖子任务是否已完成或被跳过。"""
        dep_task = graph.get(dep_id)
        if dep_task is None:
            return True  # 未知依赖视为满足
        return dep_task.status in ("done", "skipped")

    async def _execute_subtask(
        self,
        st: SubTask,
        results: Dict[str, Any],
        master_id: str,
        architecture: str,
    ) -> None:
        """执行单个子任务（含条件分支判断与多 Agent 冲突仲裁）。"""
        # 条件分支：评估条件表达式，决定是否执行
        if st.execution_mode == "conditional" and st.condition:
            try:
                should_run = bool(eval(  # noqa: S307
                    st.condition,
                    {"__builtins__": {}},
                    {"results": results},
                ))
            except Exception as exc:  # noqa: BLE001
                logger.warning("条件评估失败 (task=%s): %s", st.task_id, exc)
                should_run = False
            if not should_run:
                st.status = "skipped"
                st.result = None
                results[st.task_id] = None
                logger.info("子任务 %s 条件不满足，已跳过", st.task_id)
                return

        st.status = "running"
        agents = self._select_agents(st, master_id, architecture)
        if not agents:
            st.status = "failed"
            st.error = "无可用 Agent"
            results[st.task_id] = None
            logger.error("子任务 %s 无可用 Agent", st.task_id)
            return

        # 构建输入：子任务描述 + 依赖结果
        task_input = {
            "task_id": st.task_id,
            "description": st.description,
            "dependencies": {
                dep: results.get(dep) for dep in st.dependencies if dep in results
            },
        }

        if len(agents) == 1:
            # 单 Agent 执行
            try:
                result = await agents[0].execute(task_input)
                st.status = "done"
                st.result = result
                results[st.task_id] = result
                logger.info("子任务 %s 完成（agent=%s）", st.task_id, agents[0].agent_id)
            except Exception as exc:  # noqa: BLE001
                st.status = "failed"
                st.error = str(exc)
                results[st.task_id] = None
                logger.error("子任务 %s 执行失败: %s", st.task_id, exc, exc_info=True)
            return

        # 多 Agent 并发执行后冲突仲裁
        agent_ids = [a.agent_id for a in agents]
        gathered = await asyncio.gather(
            *(a.execute(task_input) for a in agents),
            return_exceptions=True,
        )
        outputs: Dict[str, Any] = {}
        for aid, res in zip(agent_ids, gathered):
            if isinstance(res, Exception):
                logger.warning("Agent %s 执行子任务 %s 失败: %s", aid, st.task_id, res)
            else:
                outputs[aid] = res

        if not outputs:
            st.status = "failed"
            st.error = "所有 Agent 执行失败"
            results[st.task_id] = None
            return

        if len(outputs) == 1:
            result = next(iter(outputs.values()))
        else:
            # 检测冲突并仲裁（默认投票）
            conflict = self._resolver.detect_conflicts(outputs, key=st.task_id)
            if conflict is not None:
                result = self._resolver.resolve_by_voting(conflict)
            else:
                result = next(iter(outputs.values()))

        st.status = "done"
        st.result = result
        results[st.task_id] = result
        logger.info("子任务 %s 完成（多 Agent，参与=%s）", st.task_id, list(outputs.keys()))

    def _select_agents(
        self,
        st: SubTask,
        master_id: str,
        architecture: str,
    ) -> List[Agent]:
        """为子任务选择执行 Agent 列表。"""
        # 1. 优先按分配的角色查找
        if st.assigned_role:
            agents = self.find_agents_by_role(st.assigned_role)
            if agents:
                return agents
        # 2. 主从架构：回退到主 Agent
        if architecture == "master_slave" and master_id:
            master = self._agents.get(master_id)
            if master is not None:
                return [master]
        # 3. 对等架构：回退到任意一个可用 Agent
        if self._agents:
            return [next(iter(self._agents.values()))]
        return []


# ------------------------------------------------------------ 插件类

class MultiAgentPlugin(Plugin):
    """多智能体协作引擎插件：整合角色、分解、通信、仲裁、编排与决策。"""

    name = "multi_agent"

    def __init__(self) -> None:
        super().__init__()
        self._decomposer: Optional[TaskDecomposer] = None
        self._message_bus: Optional[MessageBus] = None
        self._resolver: Optional[ConflictResolver] = None
        self._orchestrator: Optional[CollaborationOrchestrator] = None
        self._decision: Optional[GroupDecision] = None
        self._roles: Dict[str, AgentRole] = {}
        self._agents: Dict[str, Agent] = {}

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        self._decomposer = TaskDecomposer()
        self._message_bus = MessageBus()
        self._resolver = ConflictResolver()
        self._decision = GroupDecision()
        self._orchestrator = CollaborationOrchestrator(
            message_bus=self._message_bus,
            conflict_resolver=self._resolver,
        )
        # 从配置加载角色定义
        config = getattr(ctx, "config", None) or {}
        cfg = config.get("multi_agent") or {}
        for role_cfg in cfg.get("roles", []) or []:
            role = AgentRole(
                role_id=role_cfg.get("role_id", ""),
                name=role_cfg.get("name", ""),
                description=role_cfg.get("description", ""),
                system_prompt=role_cfg.get("system_prompt", ""),
                capabilities=list(role_cfg.get("capabilities", []) or []),
                tools=list(role_cfg.get("tools", []) or []),
            )
            if role.role_id:
                self._roles[role.role_id] = role
        logger.info("multi_agent plugin configured (roles=%d)", len(self._roles))

    async def start(self) -> None:
        logger.info("multi_agent plugin started")

    async def stop(self) -> None:
        logger.info("multi_agent plugin stopped")

    # -------------------------------------------- 便捷访问
    @property
    def decomposer(self) -> TaskDecomposer:
        if self._decomposer is None:
            raise RuntimeError("插件未初始化，请先调用 setup")
        return self._decomposer

    @property
    def message_bus(self) -> MessageBus:
        if self._message_bus is None:
            raise RuntimeError("插件未初始化，请先调用 setup")
        return self._message_bus

    @property
    def conflict_resolver(self) -> ConflictResolver:
        if self._resolver is None:
            raise RuntimeError("插件未初始化，请先调用 setup")
        return self._resolver

    @property
    def orchestrator(self) -> CollaborationOrchestrator:
        if self._orchestrator is None:
            raise RuntimeError("插件未初始化，请先调用 setup")
        return self._orchestrator

    @property
    def group_decision(self) -> GroupDecision:
        if self._decision is None:
            raise RuntimeError("插件未初始化，请先调用 setup")
        return self._decision

    @property
    def roles(self) -> Dict[str, AgentRole]:
        return dict(self._roles)

    @property
    def agents(self) -> Dict[str, Agent]:
        return dict(self._agents)

    # -------------------------------------------- 注册接口
    def register_role(self, role: AgentRole) -> None:
        """注册角色定义。"""
        self._roles[role.role_id] = role
        logger.info("角色已注册: %s (%s)", role.role_id, role.name)

    def register_agent(self, agent: Agent) -> None:
        """注册 Agent 实例（同时挂载到编排器与消息总线）。"""
        self._agents[agent.agent_id] = agent
        if self._orchestrator is not None:
            self._orchestrator.register_agent(agent)
        else:
            logger.warning("编排器未初始化，Agent %s 仅记录未挂载", agent.agent_id)

    # -------------------------------------------- 协作入口
    async def collaborate(
        self,
        task: str,
        architecture: str = "master_slave",
        master_id: str = "",
        strategy: str = "heuristic",
    ) -> Dict[str, Any]:
        """协作执行任务：分解 + 编排。

        Args:
            task: 待执行的复杂任务描述
            architecture: master_slave / peer_to_peer
            master_id: 主从架构下的主 Agent ID（为空时取首个已注册 Agent）
            strategy: 任务分解策略
        """
        if self._decomposer is None or self._orchestrator is None:
            raise RuntimeError("插件未初始化，请先调用 setup")
        graph = self._decomposer.decompose(task, strategy=strategy)
        if architecture == "peer_to_peer":
            return await self._orchestrator.run_peer_to_peer(graph)
        if not master_id and self._agents:
            master_id = next(iter(self._agents))
        return await self._orchestrator.run_master_slave(graph, master_id=master_id)
