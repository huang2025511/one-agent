"""Peripheral command handlers extracted from Coordinator.

These handlers are not part of the core execution flow and were
extracted as Phase 2 of the Coordinator refactoring (P0-1).
They take the coordinator as the first parameter to access shared
methods like _emit_progress and _is_zh.
"""
from __future__ import annotations

import logging

from core.agent_mesh import get_agent_mesh
from core.chart_gen import get_chart_generator
from core.conv_branch import get_branch_manager
from core.workflow_engine import get_workflow_engine
from skills.calendar import get_calendar_skill
from skills.database import get_database_skill
from skills.email import get_email_skill
from skills.mcp_server import get_mcp_server
from skills.openapi import get_openapi_skill
from .context import TurnContext

logger = logging.getLogger(__name__)


# --------------------------------------------------- Chart handler
async def handle_chart(coord, turn: TurnContext, args_text: str) -> None:
    coord._emit_progress(turn, "正在生成图表...", "chart")
    zh = coord._is_zh()
    gen = get_chart_generator()
    if not args_text.strip():
        turn.result = (
            "用法: /chart <类型> <JSON数据>\n"
            "类型: flowchart, sequence, pie, gantt, timeline, mindmap, bar, line"
            if zh else "Usage: /chart <type> <JSON data>"
        )
        return
    parts = args_text.strip().split(None, 1)
    chart_type = parts[0]
    data = {}
    if len(parts) > 1:
        import json
        try:
            data = json.loads(parts[1])
        except json.JSONDecodeError:
            pass
    turn.result = gen.generate_mermaid(chart_type, data) if chart_type in ("flowchart", "sequence", "pie", "gantt", "timeline", "mindmap") else "不支持的图表类型"
    turn.record_success(turn.result, 0)


# --------------------------------------------------- Branch handlers
async def handle_branch(coord, turn: TurnContext, args_text: str) -> None:
    coord._emit_progress(turn, "正在创建分支...", "branch")
    zh = coord._is_zh()
    mgr = get_branch_manager()
    branch_id = mgr.branch(turn.session_id, "", args_text.strip() or "branch")
    tree = mgr.get_tree(turn.session_id)
    turn.result = (
        f"已创建分支: {branch_id}\n" + tree.visualize()
        if zh else f"Branch created: {branch_id}\n" + tree.visualize()
    )
    turn.record_success(turn.result, 0)


async def handle_branch_switch(coord, turn: TurnContext, args_text: str) -> None:
    coord._emit_progress(turn, "正在切换分支...", "branch_switch")
    zh = coord._is_zh()
    mgr = get_branch_manager()
    ok = mgr.switch_branch(turn.session_id, args_text.strip())
    if ok:
        turn.result = (
            f"已切换到分支: {args_text.strip()}"
            if zh else f"Switched to branch: {args_text.strip()}"
        )
    else:
        turn.result = (
            f"分支 '{args_text.strip()}' 不存在"
            if zh else f"Branch '{args_text.strip()}' does not exist"
        )
    turn.record_success(turn.result, 0)


async def handle_branch_list(coord, turn: TurnContext, args_text: str = "") -> None:
    coord._emit_progress(turn, "正在列出分支...", "branch_list")
    mgr = get_branch_manager()
    tree = mgr.get_tree(turn.session_id)
    branches = tree.list_branches()
    if not branches:
        turn.result = "暂无分支"
    else:
        turn.result = "分支列表:\n" + "\n".join(
            f"  {'→ ' if b['is_active'] else '  '}{b['name']}: {b['messages']} 条消息"
            for b in branches
        )
    turn.record_success(turn.result, 0)


# --------------------------------------------------- Branch auto-tracking (Round 7)
def record_conversation_branch(coord, turn: TurnContext) -> None:
    """Automatically record every conversation turn in the branch tree."""
    try:
        mgr = get_branch_manager()
        tree = mgr.get_tree(turn.session_id)
        tree.add_message("user", turn.input_text[:500])
        if turn.result:
            tree.add_message("assistant", turn.result[:500])
    except Exception as exc:
        logger.debug("conv_branch auto-track failed: %s", exc)


# --------------------------------------------------- Email handler
async def handle_email(coord, turn: TurnContext, args_text: str) -> None:
    coord._emit_progress(turn, "正在处理邮件...", "email")
    zh = coord._is_zh()
    skill = get_email_skill()
    parts = args_text.strip().split(None, 1)
    action = parts[0].lower() if parts else "read"
    rest = parts[1] if len(parts) > 1 else ""

    if action == "read":
        msgs = await skill.read_inbox(limit=10)
        if not msgs:
            turn.result = "收件箱为空" if zh else "Inbox is empty"
        else:
            turn.result = "\n\n---\n\n".join(
                f"[{m.date}] {m.sender} → {m.subject}\n{m.body[:200]}"
                for m in msgs
            )
    elif action == "send":
        turn.result = "用法: /email send <收件人> <主题> <正文>" if zh else "Usage: /email send <to> <subject> <body>"
    elif action == "search":
        msgs = await skill.search(rest)
        turn.result = "\n\n---\n\n".join(
            f"[{m.date}] {m.sender} → {m.subject}" for m in msgs
        ) if msgs else "未找到" if zh else "Not found"
    else:
        turn.result = "用法: /email read|send|search" if zh else "Usage: /email read|send|search"
    turn.record_success(turn.result, 0)


# --------------------------------------------------- Calendar handler
async def handle_calendar(coord, turn: TurnContext, args_text: str) -> None:
    coord._emit_progress(turn, "正在查询日程...", "calendar")
    zh = coord._is_zh()
    skill = get_calendar_skill()
    parts = args_text.strip().split(None, 1)
    action = parts[0].lower() if parts else "list"

    if action == "list" or action == "today":
        events = await skill.list_today()
        turn.result = "今日日程:\n" + skill.format_events(events)
    elif action == "week":
        events = await skill.list_this_week()
        turn.result = "本周日程:\n" + skill.format_events(events)
    elif action == "create":
        turn.result = "用法: /calendar create <标题> <开始时间> [结束时间]" if zh else "Usage: /calendar create <title> <start> [end]"
    else:
        turn.result = "用法: /calendar list|today|week|create" if zh else "Usage: /calendar list|today|week|create"
    turn.record_success(turn.result, 0)


# --------------------------------------------------- Database handler
async def handle_db(coord, turn: TurnContext, args_text: str) -> None:
    coord._emit_progress(turn, "正在查询数据库...", "db")
    zh = coord._is_zh()
    skill = get_database_skill()
    parts = args_text.strip().split(None, 1)
    action = parts[0].lower() if parts else "tables"
    rest = parts[1] if len(parts) > 1 else ""

    if action == "tables":
        result = await skill.list_tables()
        if result.get("ok"):
            tables = [r[0] for r in result.get("rows", [])]
            turn.result = "数据库表:\n" + "\n".join(f"  - {t}" for t in tables)
        else:
            turn.result = f"获取失败: {result.get('error')}"
    elif action == "query":
        result = await skill.query(sql=rest)
        turn.result = skill._format_result(result) if result.get("ok") else f"查询失败: {result.get('error')}"
    else:
        turn.result = "用法: /db tables|query <sql>" if zh else "Usage: /db tables|query <sql>"
    turn.record_success(turn.result, 0)


# --------------------------------------------------- MCP handler
async def handle_mcp(coord, turn: TurnContext, args_text: str) -> None:
    coord._emit_progress(turn, "正在管理 MCP 服务器...", "mcp")
    zh = coord._is_zh()
    server = get_mcp_server()
    # Register current skills
    if coord._skills:
        for name, skill in coord._skills._skills.items():
            server.register_skill(name, skill)
    turn.result = (
        f"MCP Server 就绪。已注册 {len(server._skills)} 个工具。\n"
        f"使用: /mcp start 启动服务器"
        if zh else
        f"MCP Server ready. {len(server._skills)} tools registered.\n"
        f"Use: /mcp start to start the server"
    )
    turn.record_success(turn.result, 0)


# --------------------------------------------------- OpenAPI handler
async def handle_openapi(coord, turn: TurnContext, args_text: str) -> None:
    coord._emit_progress(turn, "正在加载 OpenAPI 规范...", "openapi")
    zh = coord._is_zh()
    skill = get_openapi_skill()
    parts = args_text.strip().split(None, 2)
    action = parts[0].lower() if parts else "list"

    if action == "load" and len(parts) >= 2:
        result = await skill.load_from_url(name=parts[1] if len(parts) > 1 else "default", url=parts[-1])
        if result.get("ok"):
            turn.result = f"已加载: {result['title']} ({result['endpoints_count']} 端点)"
        else:
            turn.result = f"加载失败: {result.get('error')}"
    elif action == "list":
        endpoints = skill.list_endpoints("default")
        if endpoints:
            turn.result = "\n".join(f"  {e['method']} {e['path']}" for e in endpoints[:20])
        else:
            turn.result = "未加载 API。用法: /openapi load <url>" if zh else "No API loaded. Usage: /openapi load <url>"
    else:
        turn.result = "用法: /openapi load <url> | list | search <keyword>" if zh else "Usage: /openapi load <url> | list | search <keyword>"
    turn.record_success(turn.result, 0)


# --------------------------------------------------- Agent mesh handler
async def handle_agent_mesh(coord, turn: TurnContext, args_text: str) -> None:
    coord._emit_progress(turn, "正在启动多智能体协作...", "agent_mesh")
    zh = coord._is_zh()
    if not args_text.strip():
        turn.result = (
            "用法: /mesh <复杂任务描述>\n使用多个专业Agent协作完成任务"
            if zh else "Usage: /mesh <complex task description>"
        )
        return
    if coord._llm is None:
        turn.result = "[LLM not initialized]"
        return
    mesh = get_agent_mesh(coord._llm, coord._skills)
    coord._emit_progress(turn, "多智能体协作中...", "agent_mesh")
    result = await mesh.solve(args_text.strip(), model=turn.model)
    turn.result = mesh.format_result(result)
    turn.record_success(turn.result, 0)


# --------------------------------------------------- Workflow handler
async def handle_workflow(coord, turn: TurnContext, args_text: str) -> None:
    zh = coord._is_zh()
    if not args_text.strip():
        turn.result = (
            "用法: /workflow <JSON工作流定义>\n"
            "示例: {\"name\":\"test\",\"steps\":[{\"id\":\"s1\",\"type\":\"llm_call\",\"prompt\":\"Hello\"}]}"
            if zh else "Usage: /workflow <JSON workflow definition>"
        )
        return
    if coord._llm is None:
        turn.result = "[LLM not initialized]"
        return
    import json
    try:
        workflow = json.loads(args_text.strip())
    except json.JSONDecodeError:
        turn.result = "无效的JSON格式" if zh else "Invalid JSON format"
        return
    engine = get_workflow_engine(coord._llm, coord._skills)
    coord._emit_progress(turn, "执行工作流...", "workflow")
    try:
        result = await engine.execute(workflow)
        turn.result = f"工作流完成: {result.status.value}\n耗时: {result.total_duration_ms:.0f}ms\n步骤: {len(result.steps)}"
        turn.record_success(turn.result, 0)
    except Exception as exc:
        turn.record_failure(f"workflow execution failed: {exc}")
        turn.result = f"工作流执行失败: {exc}" if zh else f"Workflow execution failed: {exc}"
