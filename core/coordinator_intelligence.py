"""Self-improvement and intelligence recording extracted from Coordinator.

Records failures for self-improvement, analyzes sentiment, tracks
user patterns, generates suggestions, and extracts topics.
Extracted as Phase 5 of the Coordinator refactoring (P0-1).
"""
from __future__ import annotations
import asyncio
import logging
import re
from typing import Any, Dict, List

from .context import TurnContext

logger = logging.getLogger(__name__)

# 保存 fire-and-forget 任务的强引用，防止被 GC 中途回收
# （CPython 文档：Task 若无强引用可能被垃圾回收器中途回收，导致
# "Task was destroyed but it is pending!" 警告且异常静默丢失）。
# 任务完成后通过 done_callback 自动从集合移除。
_background_tasks: set = set()


def _on_task_done(task: "asyncio.Task") -> None:
    """任务完成回调：从 _background_tasks 移除并记录未捕获异常。"""
    _background_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.debug("record_self_improvement_async 后台任务失败: %s", exc)


async def record_self_improvement_async(coord, turn) -> None:
    """Record failure + 用 LLM 提炼改进 + 持久化应用（真正闭环）。

    之前 _record_self_improvement 只调 record_failure 写库，
    generate_improvement / apply_improvement 从不被调用 →
    失败→学习→改进行为闭环断裂。现在：
    1. record_failure 写库
    2. 每 5 次失败触发一次 LLM 改进生成
    3. apply_improvement 持久化到 DB
    4. 下一轮 _prepare_messages 通过 get_active_improvements 注入
    """
    if turn is None:
        raise ValueError("turn cannot be None")

    if not (coord.ctx and hasattr(coord.ctx, 'self_improver') and coord.ctx.self_improver):
        return

    error_type, error_detail = coord._parse_error(turn.error)

    coord.ctx.self_improver.record_failure(
        user_input=turn.input_text,
        error_type=error_type,
        error_detail=error_detail,
        turn_meta=turn.meta,
    )

    # 每 5 次失败触发一次 LLM 改进生成（避免每次失败都调 LLM 浪费 token）
    try:
        stats = coord.ctx.self_improver.get_stats()
        total_failures = stats.get("total_failures", 0)
        if total_failures > 0 and total_failures % 5 == 0 and coord._llm is not None:
            suggestion = await coord.ctx.self_improver.generate_improvement_async(
                coord._llm,
            )
            if suggestion:
                coord.ctx.self_improver.apply_improvement("llm_analyzed", suggestion)
                logger.info("self-improvement: 已生成并应用改进建议: %s", suggestion[:80])
    except Exception as exc:
        logger.debug("self-improvement LLM 生成失败: %s", exc)


def record_self_improvement(coord, turn) -> None:
    """Record failure for self-improvement analysis（同步兼容包装）。

    Fire-and-forget: 启动异步版本，不等待结果，避免阻塞调用方。
    真正的闭环逻辑在 _record_self_improvement_async 里。
    """
    if turn is None:
        raise ValueError("turn cannot be None")
    if not (coord.ctx and hasattr(coord.ctx, 'self_improver') and coord.ctx.self_improver):
        return
    try:
        task = asyncio.create_task(record_self_improvement_async(coord, turn))
        _background_tasks.add(task)
        task.add_done_callback(_on_task_done)
    except RuntimeError:
        # 没有运行中的事件循环时，退化到同步 record_failure
        error_type, error_detail = coord._parse_error(turn.error)
        coord.ctx.self_improver.record_failure(
            user_input=turn.input_text,
            error_type=error_type,
            error_detail=error_detail,
            turn_meta=turn.meta,
        )


async def record_intelligence(coord, turn) -> None:
    """Record user preferences, sentiment, and patterns."""
    if not turn.input_text or not turn.result:
        return

    try:
        # 1. Analyze sentiment
        sentiment = coord._get_sentiment()
        analysis = await asyncio.to_thread(sentiment.analyze, turn.input_text)
        turn.meta["sentiment"] = analysis

        # 2. Record skill usage (from tool_calls in meta)
        profile = coord._get_profile()
        skills_used = turn.meta.get("skills_used", [])
        for skill in skills_used:
            success = "error" not in str(turn.result).lower()
            await asyncio.to_thread(profile.record_skill_usage, skill, success)

        # 3. Record time pattern
        await asyncio.to_thread(profile.record_time_pattern)

        # 4. Extract and record topics (simple keyword extraction)
        topics = extract_topics(turn.input_text)
        for topic in topics[:3]:
            await asyncio.to_thread(profile.record_topic, topic)

        # 5. Update language preference if detected
        if hasattr(turn, "detected_lang"):
            await asyncio.to_thread(
                profile.set_preference, "language", turn.detected_lang
            )

        # 6. Track dialog summary turn counter
        summarizer = coord._get_dialog_summarizer()
        turn_count = summarizer.increment_turn(turn.session_id)
        turn.meta["turn_count"] = turn_count

        # 7. 对话摘要 — 每 N 轮生成一次摘要，下轮注入上下文
        if summarizer.should_summarize(turn.session_id):
            try:
                # 从 turn.messages 中提取 user/assistant 对话对
                history_for_summary = []
                for msg in turn.messages:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if role == "user" and content and not content.startswith("["):
                        history_for_summary.append({"input": content, "reply": ""})
                    elif role == "assistant" and content and history_for_summary:
                        history_for_summary[-1]["reply"] = content

                if history_for_summary:
                    existing = (summarizer.get_summary(turn.session_id) or {}).get("summary", "")
                    lang = "zh" if coord._is_zh() else "en"
                    prompt = summarizer.generate_summary_prompt(
                        history_for_summary[-10:], existing, lang
                    )
                    summary_resp = await coord._llm.chat_completion(
                        messages=[{"role": "user", "content": prompt}],
                        model=coord._lightweight_model(turn),
                        temperature=0.2,
                        max_tokens=400,
                        use_cache=False,
                    )
                    summary_text = summary_resp.get("text", "").strip()
                    if summary_text:
                        summarizer.store_summary(turn.session_id, summary_text, turn_count)
                        logger.debug("dialog summary generated for session %s (%d turns)",
                                    turn.session_id, turn_count)
            except Exception as exc:
                logger.debug("dialog summary generation failed: %s", exc)

        # 8. 风格自适应学习：检测用户对回复风格的自然语言反馈
        # （"太啰嗦了"/"详细一点"/"别用 emoji"/"专业点"等），自动调整
        # StyleAdapter 并持久化到用户画像，下一轮 _prepare_messages 即生效。
        # 这让 agent 能听懂用户的风格偏好，无需任何手动配置。
        try:
            style_adapter = coord._get_style_adapter()
            updates = style_adapter.adjust_from_feedback(turn.input_text)
            if updates:
                profile = coord._get_profile()
                current = profile.get_preference("response_style") or {}
                if isinstance(current, dict):
                    current.update(updates)
                else:
                    current = dict(updates)
                await asyncio.to_thread(
                    profile.set_preference, "response_style", current
                )
                turn.meta["style_adjusted"] = updates
                logger.debug("style auto-adjusted from feedback: %s", updates)
        except Exception as exc:
            logger.debug("style auto-learning skipped: %s", exc)

    except Exception as exc:
        logger.debug("intelligence recording failed: %s", exc)


def extract_topics(text: str) -> List[str]:
    """Extract topic keywords from user input."""
    # Simple keyword-based topic extraction
    # In production, could use NLP/LLM for better extraction
    topics = []

    # Technical topics
    tech_patterns = [
        (r"代码|编程|python|javascript|java|rust", "编程"),
        (r"搜索|查找|查询|search", "搜索"),
        (r"文档|文件|file|document", "文档"),
        (r"系统|shell|命令|command", "系统"),
        (r"计算|数学|math|calc", "计算"),
        (r"图片|图像|image|photo", "图片"),
        (r"音频|语音|audio|voice", "音频"),
        (r"笔记|记录|note|save", "笔记"),
    ]

    for pattern, topic in tech_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            topics.append(topic)

    return topics


async def generate_suggestions(coord, turn) -> List[Dict[str, Any]]:
    """Generate proactive suggestions based on context."""
    try:
        suggestions_engine = coord._get_suggestions()
        skills_used = turn.meta.get("skills_used", [])
        suggestions = await asyncio.to_thread(
            suggestions_engine.generate_suggestions,
            turn.input_text,
            turn.result,
            skills_used,
            {"complexity": getattr(turn, "estimated_complexity", 0.0)},
        )
        return suggestions
    except Exception as exc:
        logger.debug("suggestion generation failed: %s", exc)
        return []
