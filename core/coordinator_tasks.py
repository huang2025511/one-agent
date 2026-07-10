"""Task state tracking sub-domain extracted from Coordinator.

Tracks multi-step tasks across turns, generates completion summaries,
and schedules follow-up checks. Extracted as Phase 4 of the
Coordinator refactoring (P0-1).
"""
from __future__ import annotations
import logging
from typing import Any

from .context import TurnContext

logger = logging.getLogger(__name__)


async def update_task_state(coord, turn):
    """Detect and track multi-step tasks across turns.

    Uses a lightweight heuristic: if the user's input contains task indicators
    (步骤, 第一步, 先...再, plan, step 1, etc.), set an active task.
    If there's already an active task, check if it's completed.
    """
    try:
        from memory.dialog_summary import get_dialog_summarizer
        summarizer = get_dialog_summarizer()
        session_id = turn.session_id
        active = summarizer.get_active_task(session_id)

        # Check if current turn completes the active task
        if active and active.status == "in_progress":
            completion_keywords = [
                "完成", "搞定", "做好了", "结束了", "done", "complete", "finished",
                "最后一步", "全部做完", "全部完成",
            ]
            input_lower = turn.input_text.lower()
            if any(kw in input_lower for kw in completion_keywords):
                completed = summarizer.complete_task(session_id)
                logger.debug("task_state: completed task '%s'", active.name)
                append_task_completion_summary(coord, turn, completed)
                return

            # Check if all steps are done
            if active.steps:
                all_done = all(s.get("status") == "done" for s in active.steps)
                if all_done:
                    completed = summarizer.complete_task(session_id)
                    logger.debug("task_state: all steps done, completed '%s'", active.name)
                    append_task_completion_summary(coord, turn, completed)
                    return

        # Detect new multi-step task from user input
        task_indicators = [
            "步骤", "第一步", "第二步", "先...再", "先...然后",
            "分几步", "step 1", "step 2", "plan", "计划",
            "流程", "分步",
        ]
        input_text = turn.input_text
        is_multi_step = any(indicator in input_text for indicator in task_indicators)

        if is_multi_step and not active:
            # Use LLM to extract task name and steps (lightweight, 1 call)
            if coord._llm:
                try:
                    zh = coord._is_zh()
                    # 深度审计 P2-6 修复：使用 response_format JSON 模式, 避免自由文本解析失败
                    prompt = (
                        "分析以下用户请求，提取任务名称和步骤。"
                        "输出 JSON 对象，字段：task_name (string, 简短任务名), "
                        "steps (array of string, 按顺序的步骤列表, 最多 8 个)。\n\n"
                        f"请求：{input_text[:500]}\n\n"
                        "JSON 输出："
                    ) if zh else (
                        "Analyze the following user request and extract task name and steps. "
                        "Output a JSON object with fields: task_name (string, short task name), "
                        "steps (array of string, ordered steps, max 8).\n\n"
                        f"Request: {input_text[:500]}\n\n"
                        "JSON output:"
                    )
                    resp = await coord._llm.chat_completion(
                        messages=[{"role": "user", "content": prompt}],
                        model=turn.model,
                        max_tokens=400,
                        tools=None,
                        temperature=0.2,
                        response_format={"type": "json_object"},
                    )
                    text = (resp.get("text") or "").strip()
                    task_name = None
                    steps: List[str] = []
                    # 优先尝试 JSON 解析; 失败则回退到行解析
                    try:
                        import json as _json
                        # 部分模型会在 JSON 外包 ```json ... ``` 围栏, 需剥离
                        cleaned = text
                        if cleaned.startswith("```"):
                            cleaned = cleaned.split("```", 2)[1]
                            if cleaned.startswith("json"):
                                cleaned = cleaned[4:]
                        obj = _json.loads(cleaned)
                        task_name = (obj.get("task_name") or "").strip()[:100]
                        raw_steps = obj.get("steps") or []
                        if isinstance(raw_steps, list):
                            steps = [str(s).strip()[:200] for s in raw_steps if str(s).strip()][:8]
                    except Exception:
                        # 回退: 行解析
                        lines = [l.strip() for l in text.split("\n") if l.strip()]
                        if lines:
                            task_name = lines[0][:100]
                            steps = lines[1:10] if len(lines) > 1 else []
                    if task_name:
                        summarizer.set_active_task(session_id, task_name, steps)
                        logger.info(
                            "task_state: detected task '%s' with %d steps",
                            task_name, len(steps),
                        )
                except Exception as exc:
                    logger.debug("task_state: LLM extraction failed: %s", exc)
    except Exception as exc:
        logger.debug("task_state: update failed: %s", exc)


def append_task_completion_summary(coord, turn, completed_task):
    """任务完成时自动生成总结报告并追加到回复末尾。

    之前 _update_task_state 检测到任务完成只记一行 debug 日志就 return，
    用户完全不知道任务被标记完成。现在自动生成结构化总结，让 agent
    主动汇报"这个多步任务做完了，各步骤结果如何"，无需用户追问。
    """
    if completed_task is None:
        return
    try:
        zh = coord._is_zh()
        name = getattr(completed_task, "name", "") or ""
        steps = getattr(completed_task, "steps", []) or []
        if not name and not steps:
            return

        if zh:
            lines = [f"\n\n---\n✅ **任务完成：{name}**"]
            if steps:
                lines.append("各步骤回顾：")
                for i, s in enumerate(steps):
                    step_name = s.get("step", "") if isinstance(s, dict) else str(s)
                    status = s.get("status", "") if isinstance(s, dict) else ""
                    result = s.get("result", "") if isinstance(s, dict) else ""
                    icon = {"done": "✅", "failed": "❌", "in_progress": "🔄"}.get(status, "⬜")
                    line = f"  {icon} {i+1}. {step_name}"
                    if result:
                        line += f" — {result[:80]}"
                    lines.append(line)
            lines.append("如需调整或继续，告诉我即可。")
        else:
            lines = [f"\n\n---\n✅ **Task completed: {name}**"]
            if steps:
                lines.append("Step recap:")
                for i, s in enumerate(steps):
                    step_name = s.get("step", "") if isinstance(s, dict) else str(s)
                    status = s.get("status", "") if isinstance(s, dict) else ""
                    result = s.get("result", "") if isinstance(s, dict) else ""
                    icon = {"done": "✅", "failed": "❌", "in_progress": "🔄"}.get(status, "⬜")
                    line = f"  {icon} {i+1}. {step_name}"
                    if result:
                        line += f" — {result[:80]}"
                    lines.append(line)
            lines.append("Let me know if you'd like to adjust or continue.")
        summary_block = "\n".join(lines)
        turn.result = (turn.result or "") + summary_block
        turn.meta["task_completion_summary"] = True
    except Exception as exc:
        logger.debug("task completion summary skipped: %s", exc)


def maybe_schedule_followup(coord, turn):
    """Schedule a follow-up check task for complex/expert work.

    Round 8：接入 AsyncTaskScheduler。如果 LLM 回复中包含"稍后"/"待跟进"/
    "I'll check back" 等关键词，注册一个延迟任务。
    用户下次发起会话时，会自动加载跟进状态。

    之前 task_scheduler 完全是死代码，定义了 AsyncTaskScheduler 但
    coordinator 从未调用。现在用于真实的 follow-up 场景。
    """
    try:
        from core.task_scheduler import get_task_scheduler
        scheduler = get_task_scheduler()
    except Exception as exc:
        logger.debug("task_scheduler not available: %s", exc)
        return

    result = (turn.result or "").lower()
    followup_indicators = [
        "稍后", "稍等", "等一下", "我稍后", "我等下", "待跟进", "待完成",
        "稍后检查", "稍后回来", "我会", "我会回来", "下次",
        "later", "check back", "follow up", "followup", "will check",
    ]
    if not any(ind in result for ind in followup_indicators):
        return

    # Round 8：使用 asyncio 调度（scheduler.schedule_delayed 是 async）
    # 这里用 ensure_future fire-and-forget，不阻塞 turn 完成
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 通过 _run_coroutine_threadsafe 在后台调度
            task_args = {
                "session_id": turn.session_id,
                "user_input": turn.input_text[:300],
            }
            # 延迟 5 分钟（300秒），检查 session 是否需要回顾
            task_id = loop.create_task(
                scheduler.schedule_delayed(
                    func_name="followup_check",
                    delay_seconds=300,
                    args=task_args,
                    name=f"followup-{turn.session_id[:20]}",
                )
            )
            coord._bg_tasks.add(task_id)
            task_id.add_done_callback(lambda t: coord._bg_tasks.discard(t))
            turn.meta["followup_scheduled"] = True
            logger.debug("followup task scheduled for session %s", turn.session_id)
    except Exception as exc:
        logger.debug("schedule_followup failed: %s", exc)


async def followup_check_handler(coord, session_id="", user_input=""):
    """延迟任务：跟进检查，主动给用户发消息询问是否需要继续。

    这是 AsyncTaskScheduler 调度的 followup_check 任务的实际处理函数。
    任务触发后，通过 bot_send_message 事件主动推送消息给用户，
    解决"一问一答"模式下用户不问就不说话的问题。

    Args:
        session_id: 会话 ID，格式为 "{gateway}-{chat_id}"（如 "wechat-xxx"）
        user_input: 用户原始输入，用于生成跟进提示
    """
    if not session_id:
        return

    # 从 session_id 解析 gateway 和 chat_id
    gateway = ""
    chat_id = ""
    if "-" in session_id:
        prefix, rest = session_id.split("-", 1)
        gateway_map = {
            "wechat": "wechat_personal",
            "wecom": "wecom",
            "telegram": "telegram",
            "dingtalk": "dingtalk",
            "feishu": "feishu",
            "discord": "discord",
            "slack": "slack",
            "web": "web",
            "cli": "cli",
        }
        gateway = gateway_map.get(prefix, prefix)
        chat_id = rest
    else:
        chat_id = session_id

    if not chat_id:
        return

    # 生成跟进消息
    zh = coord._is_zh()
    if user_input:
        preview = user_input[:40] + "..." if len(user_input) > 40 else user_input
        if zh:
            message = f"⏰ 温馨提醒\n\n关于之前的问题「{preview}」，\n您之前提到稍后继续，现在需要我接着处理吗？\n\n回复「继续」或直接说您的需求即可。"
        else:
            message = f"⏰ Reminder\n\nRegarding your previous request「{preview}」，\nyou mentioned continuing later. Would you like to proceed now?\n\nReply 'continue' or just tell me what you need."
    else:
        if zh:
            message = "⏰ 温馨提醒\n\n您之前有任务提到稍后继续，现在需要我接着处理吗？\n\n回复「继续」或直接说您的需求即可。"
        else:
            message = "⏰ Reminder\n\nYou had a task you wanted to continue later. Would you like to proceed now?\n\nReply 'continue' or just tell me what you need."

    # 发布 bot_send_message 事件，由对应网关主动推送
    try:
        from core.events import Event
        event = Event("bot_send_message", {
            "chat_id": chat_id,
            "text": message,
            "gateway": gateway,
            "source": "coordinator:followup_check",
        })
        coord.bus.publish(event)
        logger.info("coordinator: followup check sent to %s (gateway=%s)", chat_id[:8], gateway)
    except Exception as exc:
        logger.warning("coordinator: followup check send failed: %s", exc)
