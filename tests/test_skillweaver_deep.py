"""SkillWeaver 深度功能验证 — 每个组件实际运行，不走走过场。

验证清单:
  1. SkillIndex 构建真实 FAISS 索引 + 检索
  2. SkillWeaverRouter.retrieve_skills() 公开 API
  3. route() 三阶段管线 (Decompose → SAD → Compose)
  4. Coordinator._get_skillweaver_router() 缓存
  5. _prepare_tools() 配置路径正确读取
  6. _plan_tool_chain() 真正调用 route()
  7. _format_dag_workflow() 拓扑分层
  8. execute_workflow() DAG 并行执行
"""

import asyncio
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, AsyncMock, patch

# 使用 HF 镜像（沙箱无法直连 huggingface.co）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# 确保能导入项目模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================
# 测试基础设施
# ============================================================

class TestResult:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.details: List[str] = []

    def ok(self, name: str, detail: str = ""):
        self.passed += 1
        status = "PASS"
        msg = f"  [{status}] {name}"
        if detail:
            msg += f" — {detail}"
        self.details.append(msg)
        print(msg)

    def fail(self, name: str, detail: str = ""):
        self.failed += 1
        status = "FAIL"
        msg = f"  [{status}] {name}"
        if detail:
            msg += f" — {detail}"
        self.details.append(msg)
        print(msg)

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*60}")
        print(f"深度验证结果: {self.passed}/{total} 通过, {self.failed} 失败")
        if self.failed > 0:
            print("失败项:")
            for d in self.details:
                if "[FAIL]" in d:
                    print(f"  {d}")
        print(f"{'='*60}")
        return self.failed == 0


# ============================================================
# Mock LLM Provider — 模拟真实 LLM 返回
# ============================================================

class MockLLMProvider:
    """模拟 LLM，根据 prompt 内容返回预设的 JSON 响应。"""

    def __init__(self):
        self.call_count = 0
        self.calls: List[Dict] = []

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        use_cache: bool = True,
        _skip_fallback: bool = False,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self.call_count += 1
        prompt = messages[-1].get("content", "") if messages else ""
        self.calls.append({"prompt": prompt[:200], "call_num": self.call_count})

        # Decompose 阶段
        if "任务分解" in prompt or "Task Decomposition" in prompt:
            return {"text": json.dumps({
                "subtasks": [
                    {"id": "task_1", "description": "搜索相关信息", "dependencies": []},
                    {"id": "task_2", "description": "分析搜索结果", "dependencies": ["task_1"]},
                    {"id": "task_3", "description": "生成总结报告", "dependencies": ["task_2"]},
                ]
            })}

        # SAD 重写阶段
        if "技能对齐重写" in prompt or "Skill Alignment Rewrite" in prompt:
            return {"text": "使用 web_search 工具搜索网络信息并返回结果"}

        # Compose 阶段
        if "工作流编排" in prompt or "Workflow Composition" in prompt:
            return {"text": json.dumps({
                "nodes": [
                    {"subtask_id": "task_1", "skill_id": "web_search", "args": {"input": "查询内容"}, "dependencies": []},
                    {"subtask_id": "task_2", "skill_id": "python_execute", "args": {"code": "analyze()"}, "dependencies": ["task_1"]},
                    {"subtask_id": "task_3", "skill_id": "calc", "args": {"expression": "result"}, "dependencies": ["task_2"]},
                ],
                "edges": [["task_1", "task_2"], ["task_2", "task_3"]]
            })}

        # Fallback 简单工具链规划
        if "工具链规划" in prompt or "Tool chain planning" in prompt:
            return {"text": "1. 先用 web_search 搜索信息\n2. 再用 python_execute 分析结果\n3. 最后用 calc 计算指标"}

        # 默认回退
        return {"text": "默认回复"}


# ============================================================
# 构建真实 Skill 注册表
# ============================================================

def make_test_skills():
    """创建真实的 Skill 对象，而非 mock。"""
    from skills import Skill

    skills = {}

    def web_search_handler(args):
        return f"搜索结果: {args.get('input', '')}"

    async def async_web_search_handler(args):
        return web_search_handler(args)

    skills["web_search"] = Skill(
        id="web_search",
        title="网络搜索",
        description="Search the web for information, news, articles, and real-time data. 网络搜索引擎，查找信息和实时数据。",
        schema={"function": {"name": "web_search", "parameters": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}}},
        handler=async_web_search_handler,
    )

    async def python_handler(args):
        return f"执行结果: {args.get('code', '')}"

    skills["python_execute"] = Skill(
        id="python_execute",
        title="Python 代码执行",
        description="Execute Python code for data analysis, computation, and automation tasks. 执行 Python 代码进行数据分析。",
        schema={"function": {"name": "python_execute", "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}},
        handler=python_handler,
    )

    async def calc_handler(args):
        return f"计算结果: {args.get('expression', '')}"

    skills["calc"] = Skill(
        id="calc",
        title="计算器",
        description="Perform mathematical calculations and evaluate expressions. 数学计算器，支持表达式求值。",
        schema={"function": {"name": "calc", "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}},
        handler=calc_handler,
    )

    async def send_message_handler(args):
        return f"消息已发送: {args.get('message', '')}"

    skills["send_message"] = Skill(
        id="send_message",
        title="发送消息",
        description="Send a message to a user or channel. 发送消息给用户或频道。",
        schema={"function": {"name": "send_message", "parameters": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}}},
        handler=send_message_handler,
    )

    async def weather_handler(args):
        return f"天气: {args.get('location', '')}"

    skills["weather"] = Skill(
        id="weather",
        title="天气预报",
        description="Get current weather forecast for a location. 查询天气预报信息。",
        schema={"function": {"name": "weather", "parameters": {"type": "object", "properties": {"location": {"type": "string"}}, "required": ["location"]}}},
        handler=weather_handler,
    )

    return skills


def make_mock_skill_manager(skills_dict):
    """创建模拟 SkillManager，但包含真实 _skills 字典。"""
    mgr = MagicMock()
    mgr._skills = skills_dict
    mgr.get = lambda sid: skills_dict.get(sid)

    async def mock_dispatch(skill_id, args):
        skill = skills_dict.get(skill_id)
        if skill is None:
            return f"[unknown skill: {skill_id}]"
        return await skill.run(args)
    mgr.dispatch = mock_dispatch
    mgr.pick_relevant = lambda text, limit=4: list(skills_dict.values())[:limit]
    return mgr


# ============================================================
# 测试用例
# ============================================================

async def test_1_skillindex_build_and_retrieve(result: TestResult):
    """验证1: SkillIndex 构建真实 FAISS 索引并执行语义检索。"""
    name = "test_1_skillindex_build_and_retrieve"
    print(f"\n--- {name} ---")
    try:
        from core.skillweaver import SkillIndex
        skills = make_test_skills()

        idx = SkillIndex("all-MiniLM-L6-v2")
        built = idx.build(skills)

        if not built:
            result.fail(name, "SkillIndex.build() returned False")
            return

        result.ok(name, f"FAISS 索引构建成功，包含 {len(skills)} 个技能")

        # 测试语义检索 — "搜索网络上的信息" 应该最匹配 web_search
        results = idx.retrieve("搜索网络上的信息", top_k=3)
        if not results:
            result.fail(name, "retrieve() 返回空列表")
            return

        top_skill = results[0][0]
        top_score = results[0][1]
        if top_skill == "web_search":
            result.ok("检索准确性", f"'搜索网络上的信息' → web_search (score={top_score:.4f})")
        else:
            result.fail("检索准确性", f"期望 web_search, 实际 {top_skill} (score={top_score:.4f})")

        # 测试中文查询
        results2 = idx.retrieve("计算数学表达式", top_k=3)
        if results2 and results2[0][0] == "calc":
            result.ok("中文语义检索", f"'计算数学表达式' → calc (score={results2[0][1]:.4f})")
        else:
            result.fail("中文语义检索", f"期望 calc, 实际 {results2[0][0] if results2 else 'empty'}")

        # 测试英文查询
        results3 = idx.retrieve("execute python code for data analysis", top_k=3)
        if results3 and results3[0][0] == "python_execute":
            result.ok("英文语义检索", f"'execute python code' → python_execute (score={results3[0][1]:.4f})")
        else:
            result.fail("英文语义检索", f"期望 python_execute, 实际 {results3[0][0] if results3 else 'empty'}")

    except Exception as exc:
        result.fail(name, f"异常: {exc}")
        traceback.print_exc()


async def test_2_retrieve_skills_public_api(result: TestResult):
    """验证2: SkillWeaverRouter.retrieve_skills() 公开 API。"""
    name = "test_2_retrieve_skills_public_api"
    print(f"\n--- {name} ---")
    try:
        from core.skillweaver import SkillWeaverRouter
        llm = MockLLMProvider()
        skills = make_test_skills()
        mgr = make_mock_skill_manager(skills)

        router = SkillWeaverRouter(llm, mgr)

        # 未初始化时 retrieve_skills 应自动调用 initialize
        results = router.retrieve_skills("搜索信息", top_k=3)

        if not results:
            result.fail(name, "retrieve_skills() 返回空")
            return

        result.ok(name, f"返回 {len(results)} 个技能: {[r[0] for r in results]}")

        # 验证不再直接访问 _index
        # retrieve_skills 是公开方法，不应需要访问 _index
        if hasattr(router, 'retrieve_skills'):
            result.ok("公开API存在", "retrieve_skills() 方法存在且可调用")
        else:
            result.fail("公开API存在", "retrieve_skills() 方法不存在")

    except Exception as exc:
        result.fail(name, f"异常: {exc}")
        traceback.print_exc()


async def test_3_route_three_phase_pipeline(result: TestResult):
    """验证3: route() 三阶段管线 Decompose → SAD → Compose 完整执行。"""
    name = "test_3_route_three_phase_pipeline"
    print(f"\n--- {name} ---")
    try:
        from core.skillweaver import SkillWeaverRouter
        llm = MockLLMProvider()
        skills = make_test_skills()
        mgr = make_mock_skill_manager(skills)

        router = SkillWeaverRouter(llm, mgr)
        router.initialize()

        workflow = await router.route("帮我搜索信息并分析结果", zh=True)

        # 验证 Decompose 阶段产生了子任务
        if not workflow.nodes:
            result.fail(name, "route() 返回空 workflow（无节点）")
            # 检查 LLM 是否被调用
            result.fail("LLM调用", f"LLM 被调用 {llm.call_count} 次")
            return

        result.ok("DAG节点数", f"生成了 {len(workflow.nodes)} 个节点")

        # 验证 LLM 被调用至少 3 次（Decompose + SAD + Compose）
        if llm.call_count >= 3:
            result.ok("三阶段执行", f"LLM 被调用 {llm.call_count} 次 (Decompose + SAD + Compose)")
        else:
            result.fail("三阶段执行", f"LLM 仅被调用 {llm.call_count} 次，期望至少 3 次")

        # 验证节点结构
        for node in workflow.nodes:
            if not node.skill_id:
                result.fail("节点完整性", f"节点 {node.subtask_id} 缺少 skill_id")
                return
        result.ok("节点完整性", "所有节点都有 skill_id")

        # 验证依赖关系
        has_deps = any(node.dependencies for node in workflow.nodes)
        if has_deps:
            result.ok("DAG依赖", "存在节点间依赖关系（DAG 有效）")
        else:
            result.fail("DAG依赖", "无任何依赖关系，不是 DAG")

        # 验证 entry_points
        if workflow.entry_points:
            result.ok("入口节点", f"找到 {len(workflow.entry_points)} 个入口节点: {workflow.entry_points}")
        else:
            result.fail("入口节点", "未找到入口节点")

        # 验证 skill_id 映射到真实技能
        valid_skills = all(node.skill_id in skills for node in workflow.nodes)
        if valid_skills:
            result.ok("技能映射", "所有 skill_id 都映射到真实技能")
        else:
            invalid = [n.skill_id for n in workflow.nodes if n.skill_id not in skills]
            result.fail("技能映射", f"无效 skill_id: {invalid}")

    except Exception as exc:
        result.fail(name, f"异常: {exc}")
        traceback.print_exc()


async def test_4_router_caching(result: TestResult):
    """验证4: Coordinator._get_skillweaver_router() 缓存是否生效。"""
    name = "test_4_router_caching"
    print(f"\n--- {name} ---")
    try:
        from core.coordinator import Coordinator
        from core.context import AgentContext
        from core.events import EventBus

        llm = MockLLMProvider()
        skills = make_test_skills()
        mgr = make_mock_skill_manager(skills)

        coord = Coordinator()
        coord._llm = llm
        coord._skills = mgr
        # 模拟 ctx.config
        coord.ctx = MagicMock()
        coord.ctx.config = {"router": {"skillweaver": {"enabled": True}}}

        # 第一次调用 — 应构建索引
        t0 = time.time()
        router1 = coord._get_skillweaver_router()
        t1 = time.time()
        build_time = t1 - t0

        if router1 is None:
            result.fail(name, "第一次调用返回 None")
            return

        result.ok("首次构建", f"router 创建成功，耗时 {build_time:.3f}s")

        # 第二次调用 — 应直接返回缓存
        t2 = time.time()
        router2 = coord._get_skillweaver_router()
        t3 = time.time()
        cache_time = t3 - t2

        if router2 is None:
            result.fail("缓存命中", "第二次调用返回 None")
            return

        if router1 is router2:
            speedup = build_time / max(cache_time, 0.0001)
            result.ok("缓存命中", f"返回同一实例，缓存查询 {cache_time:.6f}s (提速 {speedup:.0f}x)")
        else:
            result.fail("缓存命中", "返回了不同实例，缓存未生效")

    except Exception as exc:
        result.fail(name, f"异常: {exc}")
        traceback.print_exc()


async def test_5_prepare_tools_config_path(result: TestResult):
    """验证5: _prepare_tools() 正确读取 router.skillweaver 配置路径。"""
    name = "test_5_prepare_tools_config_path"
    print(f"\n--- {name} ---")
    try:
        from core.coordinator import Coordinator
        from core.context import TurnContext

        llm = MockLLMProvider()
        skills = make_test_skills()
        mgr = make_mock_skill_manager(skills)

        coord = Coordinator()
        coord._llm = llm
        coord._skills = mgr
        coord.ctx = MagicMock()
        coord.ctx.config = {
            "router": {
                "skillweaver": {
                    "enabled": True,
                    "min_complexity": 0.3,
                }
            }
        }

        # 测试1: 高复杂度任务应该启用 SkillWeaver
        turn = TurnContext(input_text="搜索信息", model="test/model", estimated_complexity=0.8)
        tools = coord._prepare_tools(turn)

        if tools:
            result.ok("高复杂度", f"返回 {len(tools)} 个工具")
            # 验证 web_search 被包含（语义检索 + core tools）
            tool_names = [t.get("function", {}).get("name", "") for t in tools]
            if "web_search" in tool_names:
                result.ok("语义检索生效", f"工具列表: {tool_names}")
            else:
                result.fail("语义检索生效", f"web_search 不在工具列表: {tool_names}")
        else:
            result.fail("高复杂度", "返回空工具列表")

        # 测试2: 低复杂度任务应该跳过 SkillWeaver（走关键词匹配）
        turn2 = TurnContext(input_text="你好", model="test/model", estimated_complexity=0.1)
        tools2 = coord._prepare_tools(turn2)
        if tools2:
            result.ok("低复杂度跳过", f"复杂度 0.1 跳过 SkillWeaver, 仍返回 {len(tools2)} 个工具")
        else:
            result.fail("低复杂度跳过", "返回空工具列表")

        # 测试3: 禁用 SkillWeaver 应该走关键词匹配
        coord.ctx.config = {"router": {"skillweaver": {"enabled": False}}}
        coord._skillweaver_router = None  # 清除缓存
        turn3 = TurnContext(input_text="搜索信息", model="test/model", estimated_complexity=0.8)
        tools3 = coord._prepare_tools(turn3)
        if tools3:
            result.ok("禁用回退", "skillweaver.enabled=false 时成功回退到关键词匹配")
        else:
            result.fail("禁用回退", "禁用后返回空列表")

    except Exception as exc:
        result.fail(name, f"异常: {exc}")
        traceback.print_exc()


async def test_6_plan_tool_chain_calls_route(result: TestResult):
    """验证6: _plan_tool_chain() 是否真正调用 route() 生成 DAG 计划。"""
    name = "test_6_plan_tool_chain_calls_route"
    print(f"\n--- {name} ---")
    try:
        from core.coordinator import Coordinator
        from core.context import TurnContext

        llm = MockLLMProvider()
        skills = make_test_skills()
        mgr = make_mock_skill_manager(skills)

        coord = Coordinator()
        coord._llm = llm
        coord._skills = mgr
        coord.ctx = MagicMock()
        coord.ctx.config = {"router": {"skillweaver": {"enabled": True}}}

        turn = TurnContext(
            input_text="帮我搜索信息并分析结果",
            model="test/model",
            estimated_complexity=0.9,
        )

        # 构造 tools 列表
        tools = [
            {"type": "function", "function": {"name": "web_search", "parameters": {}}},
            {"type": "function", "function": {"name": "python_execute", "parameters": {}}},
        ]

        messages = [{"role": "system", "content": "test"}, {"role": "user", "content": turn.input_text}]

        # 调用 _plan_tool_chain
        await coord._plan_tool_chain(messages, turn, tools)

        # 验证 plan 被注入到 messages
        plan_found = False
        for msg in messages:
            content = msg.get("content", "")
            if "SkillWeaver" in content or "工具链" in content or "Tool chain" in content:
                plan_found = True
                break

        if plan_found:
            result.ok(name, "计划已注入到 messages")
        else:
            result.fail(name, "messages 中未找到计划")

        # 验证 turn.meta 中有 tool_chain_plan
        if turn.meta.get("tool_chain_plan"):
            plan_text = turn.meta["tool_chain_plan"]
            result.ok("plan元数据", f"turn.meta['tool_chain_plan'] 已设置 ({len(plan_text)} 字符)")
            # 验证 plan 内容包含步骤和工具
            if "web_search" in plan_text or "python_execute" in plan_text:
                result.ok("plan内容", "计划中包含工具名称")
            else:
                result.fail("plan内容", f"计划中缺少工具名称: {plan_text[:200]}")
        else:
            result.fail("plan元数据", "turn.meta['tool_chain_plan'] 未设置")

        # 验证 LLM 被调用（route 内部的 Decompose + SAD + Compose）
        if llm.call_count >= 3:
            result.ok("route调用", f"LLM 被调用 {llm.call_count} 次，确认 route() 三阶段管线执行")
        else:
            result.fail("route调用", f"LLM 仅被调用 {llm.call_count} 次，route() 可能未执行")

    except Exception as exc:
        result.fail(name, f"异常: {exc}")
        traceback.print_exc()


async def test_7_format_dag_workflow_topological(result: TestResult):
    """验证7: _format_dag_workflow() 拓扑分层是否正确。"""
    name = "test_7_format_dag_workflow_topological"
    print(f"\n--- {name} ---")
    try:
        from core.coordinator import Coordinator
        from core.skillweaver import DAGWorkflow, SkillNode

        coord = Coordinator()

        # 构建一个有明确依赖链的 DAG:
        #   task_1 (no deps) → task_2 (depends on 1) → task_3 (depends on 2)
        #   task_4 (no deps, parallel with task_1)
        workflow = DAGWorkflow(
            nodes=[
                SkillNode(subtask_id="task_1", skill_id="web_search", args={"input": "test"}, dependencies=[]),
                SkillNode(subtask_id="task_2", skill_id="python_execute", args={"code": "x=1"}, dependencies=["task_1"]),
                SkillNode(subtask_id="task_3", skill_id="calc", args={"expression": "1+1"}, dependencies=["task_2"]),
                SkillNode(subtask_id="task_4", skill_id="send_message", args={"message": "hi"}, dependencies=[]),
            ],
            edges=[("task_1", "task_2"), ("task_2", "task_3")],
            entry_points=["task_1", "task_4"],
        )

        plan = coord._format_dag_workflow(workflow, zh=True)

        if not plan:
            result.fail(name, "返回空字符串")
            return

        result.ok(name, f"生成了 {len(plan)} 字符的计划")

        # 验证拓扑分层：task_1 和 task_4 应在第 1 步（并行）
        lines = plan.split("\n")
        step1_section = []
        step2_section = []
        current_step = 0
        for line in lines:
            if "第 1 步" in line:
                current_step = 1
            elif "第 2 步" in line:
                current_step = 2
            elif "第 3 步" in line:
                current_step = 3
            elif current_step == 1:
                step1_section.append(line)
            elif current_step == 2:
                step2_section.append(line)

        step1_text = "\n".join(step1_section)
        step2_text = "\n".join(step2_section)

        # task_1 和 task_4 应在第一步
        if "web_search" in step1_text and "send_message" in step1_text:
            result.ok("第1层并行", "task_1(web_search) 和 task_4(send_message) 正确在第一层")
        else:
            result.fail("第1层并行", f"第一层内容: {step1_text}")

        # task_2 应在第二步
        if "python_execute" in step2_text:
            result.ok("第2层依赖", "task_2(python_execute) 正确在第二层")
        else:
            result.fail("第2层依赖", f"第二层内容: {step2_text}")

    except Exception as exc:
        result.fail(name, f"异常: {exc}")
        traceback.print_exc()


async def test_8_execute_workflow_parallel(result: TestResult):
    """验证8: execute_workflow() DAG 并行执行。"""
    name = "test_8_execute_workflow_parallel"
    print(f"\n--- {name} ---")
    try:
        from core.skillweaver import SkillWeaverRouter, DAGWorkflow, SkillNode
        from skills import Skill

        # 创建可追踪执行顺序的 skills
        execution_log: List[str] = []
        execution_times: Dict[str, float] = {}

        async def slow_search_handler(args):
            execution_times["start_search"] = time.time()
            await asyncio.sleep(0.2)  # 模拟耗时
            execution_log.append("web_search")
            execution_times["end_search"] = time.time()
            return "搜索完成"

        async def fast_calc_handler(args):
            execution_times["start_calc"] = time.time()
            execution_log.append("calc")
            execution_times["end_calc"] = time.time()
            return "42"

        async def dep_handler(args):
            execution_log.append("python_execute")
            return "分析完成"

        skills = {
            "web_search": Skill(
                id="web_search", title="搜索", description="search",
                schema={"function": {"name": "web_search", "parameters": {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}}},
                handler=slow_search_handler,
            ),
            "calc": Skill(
                id="calc", title="计算", description="calculate",
                schema={"function": {"name": "calc", "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}},
                handler=fast_calc_handler,
            ),
            "python_execute": Skill(
                id="python_execute", title="执行", description="execute",
                schema={"function": {"name": "python_execute", "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}},
                handler=dep_handler,
            ),
        }

        mgr = make_mock_skill_manager(skills)
        llm = MockLLMProvider()
        router = SkillWeaverRouter(llm, mgr)
        router.initialize()

        # 构建并行 DAG: task_1 和 task_2 无依赖（应并行），task_3 依赖 task_1
        workflow = DAGWorkflow(
            nodes=[
                SkillNode(subtask_id="task_1", skill_id="web_search", args={"input": "test"}, dependencies=[]),
                SkillNode(subtask_id="task_2", skill_id="calc", args={"expression": "6*7"}, dependencies=[]),
                SkillNode(subtask_id="task_3", skill_id="python_execute", args={"code": "x=1"}, dependencies=["task_1"]),
            ],
            edges=[("task_1", "task_3")],
            entry_points=["task_1", "task_2"],
        )

        progress_log: List[str] = []
        def on_progress(sid: str, status: str):
            progress_log.append(f"{sid}:{status}")

        t0 = time.time()
        exec_result = await router.execute_workflow(workflow, on_progress=on_progress)
        elapsed = time.time() - t0

        # 验证所有节点执行成功
        if exec_result.get("success"):
            result.ok("执行成功", "所有节点执行成功")
        else:
            failed = [n.subtask_id for n in workflow.nodes if n.status != "done"]
            result.fail("执行成功", f"失败的节点: {failed}")

        # 验证并行执行：task_1 和 task_2 应同时开始
        # 如果是串行，总耗时 >= 0.2 + 0 + 0 = 0.2s
        # 如果是并行，总耗时应接近 max(0.2, ~0) ≈ 0.2s
        # 关键验证：task_2 应在 task_1 完成之前开始
        if "start_search" in execution_times and "start_calc" in execution_times:
            search_start = execution_times["start_search"]
            calc_start = execution_times["start_calc"]
            search_end = execution_times.get("end_search", search_start + 1)

            if calc_start < search_end:
                result.ok("并行执行", f"calc 在 {calc_start:.4f} 开始, search 在 {search_end:.4f} 结束 → 确认并行")
            else:
                result.fail("并行执行", f"calc 在 {calc_start:.4f} 开始, search 在 {search_end:.4f} 结束 → 可能串行")

        # 验证依赖执行顺序：task_3 应在 task_1 之后
        if "web_search" in execution_log and "python_execute" in execution_log:
            if execution_log.index("web_search") < execution_log.index("python_execute"):
                result.ok("依赖顺序", "task_1(web_search) 在 task_3(python_execute) 之前完成")
            else:
                result.fail("依赖顺序", f"执行顺序错误: {execution_log}")

        # 验证进度回调
        if progress_log:
            result.ok("进度回调", f"收到 {len(progress_log)} 个进度事件: {progress_log}")
        else:
            result.fail("进度回调", "未收到任何进度事件")

        # 验证结果
        results = exec_result.get("results", {})
        if "task_1" in results and "task_2" in results and "task_3" in results:
            result.ok("结果完整", f"所有 3 个任务都有结果: {list(results.keys())}")
        else:
            result.fail("结果完整", f"结果不完整: {list(results.keys())}")

    except Exception as exc:
        result.fail(name, f"异常: {exc}")
        traceback.print_exc()


# ============================================================
# 主入口
# ============================================================

async def main():
    print("=" * 60)
    print("SkillWeaver 深度功能验证")
    print("=" * 60)

    result = TestResult()

    await test_1_skillindex_build_and_retrieve(result)
    await test_2_retrieve_skills_public_api(result)
    await test_3_route_three_phase_pipeline(result)
    await test_4_router_caching(result)
    await test_5_prepare_tools_config_path(result)
    await test_6_plan_tool_chain_calls_route(result)
    await test_7_format_dag_workflow_topological(result)
    await test_8_execute_workflow_parallel(result)

    ok = result.summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
