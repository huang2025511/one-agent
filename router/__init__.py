"""Smart router — the "SquillaRouter" from OpenSquilla's 4-layer design.

Layers:
    1. Task complexity classifier  → pick model tier (trivial..expert)
    2. Context compression         → avoid feeding the full history every turn
    3. Skill lazy loading          → only keep N skills in context
    4. Self-evolution              → tune thresholds from turn outcomes

The router subscribes to ``user_message``, mutates the TurnContext, and
publishes ``turn_routed``.  Something downstream is then responsible for
calling the LLM and publishing ``turn_completed``.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List

from core.context import TurnContext
from core.events import Event
from core.plugin import Plugin
from models import LLMProvider
from models.tiers import MODEL_TIERS

logger = logging.getLogger(__name__)

# Default complexity thresholds for task classification
DEFAULT_TRIVIAL_THRESHOLD = 0.2
DEFAULT_SIMPLE_THRESHOLD = 0.5
DEFAULT_COMPLEX_THRESHOLD = 0.8
MAX_COMPLEXITY = 100


_KEYWORDS_BY_TIER = {
    "trivial": re.compile(
        r"\b(hi|hello|hey|ok|thanks|thank you|what's|what is|when|where|who|"
        r"天气|时间|日期|你好|谢谢|再见|help|\?)\b",
        re.IGNORECASE,
    ),
    "expert": re.compile(
        r"\b(optimize|performance|debug|deadlock|coredump|algorithm|prover|"
        r"mathematical proof|formal verify|ml training|reverse engineer|"
        r"审计|优化|性能|死锁|算法|数学|证明|反编译)\b",
        re.IGNORECASE,
    ),
}

_CODE_HINT = re.compile(
    r"(python|javascript|typescript|rust|c\+\+|java|go|golang|shell|"
    r"bash|node|docker|kubernetes|代码|编程|代码示例)",
    re.IGNORECASE,
)


class SmartRouter(Plugin):
    """Routes each turn to the cheapest capable model + compresses context."""

    name = "router"
    depends_on = ["llm"]

    def __init__(self) -> None:
        super().__init__()
        self._cfg: Dict[str, Any] = {}
        self._history: List[Dict[str, Any]] = []
        self._llm: LLMProvider | None = None
        self._session_history: Dict[str, Any] = {}
        # running accuracy counters per-tier (for self-evolution)
        self._tier_stats: Dict[str, Dict[str, int]] = {
            t: {"picked": 0, "rerouted_up": 0, "rerouted_down": 0}
            for t in MODEL_TIERS
        }

    # -------------------------------------------------------- lifecycle
    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        self._cfg = ctx.config.get("router", {}) or {}
        assert self._cfg is not None, "Router configuration must be initialized"
        # LLM will be bound via bind_llm() call in OneAgentApp.start()
        self._session_history: Dict[str, Any] = {}
        self._max_sessions = 500  # cap total sessions to prevent unbounded growth
        self.bus.subscribe("user_message", self._on_user_message)
        self.bus.subscribe("turn_completed", self._on_turn_completed)
        self.bus.subscribe("turn_completed", self._on_done)
        self.bus.subscribe("cron", self._on_cron)
        logger.info("router configured (compression=%s, self_evo=%s)",
                    self._cfg.get("context_compression", {}).get("enabled", True),
                    self._cfg.get("self_evolution", {}).get("enabled", True))

    # ------------------------------------------------------------ API
    def bind_llm(self, provider: LLMProvider) -> None:
        self._llm = provider

    # -------------------------------------------------------- handlers
    async def _on_user_message(self, event: Event) -> None:
        turn: TurnContext | None = event.get("turn")
        if turn is None or turn.result is not None:
            return
        # 1) classify complexity
        turn.estimated_complexity = self._classify(turn.input_text)
        tier = self._tier_for_complexity(turn.estimated_complexity)
        model = self._llm.model_for_tier(tier) if self._llm else None
        turn.model = model  # None is fine — chat_completion falls back to default model
        # 2) build messages + compress history
        turn.messages = self._build_messages(turn)
        # 3) pass context_compression config to coordinator via turn.meta
        compression_cfg = self._cfg.get("context_compression", {}) or {}
        turn.meta["context_compression"] = compression_cfg.get("enabled", True)
        # 4) cap token budget
        turn.token_budget = self._token_budget_for(tier)
        self._tier_stats[tier]["picked"] += 1
        logger.info("router: complexity=%.2f tier=%s model=%s",
                    turn.estimated_complexity, tier, turn.model)
        self.publish("turn_routed", turn=turn)

    async def _on_turn_completed(self, event: Event) -> None:
        """Self-evolution hook.  Record outcomes; adjust thresholds once a
        reasonable amount of data is available.
        """
        turn: TurnContext | None = event.get("turn")
        if turn is None:
            return
        self._history.append({
            "t": time.time(),
            "complexity": turn.estimated_complexity,
            "model": turn.model,
            "tokens": turn.tokens_used,
            "duration": turn.duration_seconds,
            "failed": bool(turn.error),
        })
        if len(self._history) > 5000:
            self._history = self._history[-5000:]

        # Dynamic threshold adjustment: every ~50 routed turns, evaluate
        # per-tier failure rate and nudge thresholds accordingly.
        self_evo_cfg = self._cfg.get("self_evolution", {}) or {}
        if not self_evo_cfg.get("enabled", True):
            return
        total_routed = sum(s["picked"] for s in self._tier_stats.values())
        eval_interval = self_evo_cfg.get("eval_interval", 50)
        # 防御：eval_interval=0 会导致 ZeroDivisionError（被 EventBus 捕获
        # 后自演化逻辑永久停摆），且会触发每次 turn 都调整阈值的过频问题。
        if eval_interval <= 0:
            logger.warning(
                "router.self_evolution.eval_interval=%s 无效，已重置为 50",
                eval_interval,
            )
            eval_interval = 50
        if total_routed > 0 and total_routed % eval_interval == 0:
            self._adjust_thresholds()

    async def _on_done(self, event: Event) -> None:
        turn: TurnContext | None = event.get("turn")
        if turn is None:
            return
        sid = turn.session_id
        hist = self._session_history.setdefault(sid, [])
        # Skip failed turns — None reply pollutes conversation history
        # and breaks message alternation (user→assistant→user→assistant).
        # If reply is still None after error fallback, use a placeholder so
        # the assistant turn is always represented in history.
        reply = turn.result
        if reply is None and turn.error is not None:
            reply = f"[error: {turn.error}]" if turn.error else None
        if reply is None:
            # No result and no error: use a neutral placeholder to preserve
            # the user→assistant alternation required by most providers.
            reply = "(无响应)"
        hist.append({"input": turn.input_text, "reply": reply})
        # Bounds check: ensure we don't keep more than 20 entries
        if len(hist) > 20:
            self._session_history[sid] = hist[-20:]
        else:
            self._session_history[sid] = hist
        # global cap: evict oldest session when total exceeds limit
        if len(self._session_history) > self._max_sessions:
            oldest_key = next(iter(self._session_history))
            del self._session_history[oldest_key]

    async def _on_cron(self, event: Event) -> None:
        """Handle router_statistics: log tier distribution summary."""
        job_name = event.get("name") or ""
        if job_name == "router_statistics":
            logger.info("router statistics: tier distribution=%s", self._tier_stats)

    # --------------------------------------------------------- internal
    def _classify(self, text: str) -> float:
        """Heuristic 0..1 complexity score.

        Keyword-only classifier — cheap, deterministic, and good enough for
        the common case.  We intentionally keep it tiny so router overhead
        never dominates the LLM bill.
        """
        t = text.strip()
        if not t:
            return 0.0
        score = 0.1
        length = len(t)
        if length > 400:
            score += 0.25
        if length > 1500:
            score += 0.25
        if _KEYWORDS_BY_TIER["expert"].search(t):
            score += 0.35
        if _CODE_HINT.search(t):
            score += 0.15
        if _KEYWORDS_BY_TIER["trivial"].search(t) and length < 60:
            score -= 0.3
        # multiple sentences / paragraphs → higher
        paragraphs = sum(1 for p in t.split("\n") if p.strip())
        score += min(0.1, paragraphs * 0.02)
        return max(0.0, min(1.0, score))

    def _tier_for_complexity(self, c: float) -> str:
        thresholds = self._cfg.get("task_complexity_thresholds", {}) or {}
        if c < thresholds.get("trivial", DEFAULT_TRIVIAL_THRESHOLD):
            return "trivial"
        if c < thresholds.get("simple", DEFAULT_SIMPLE_THRESHOLD):
            return "simple"
        if c < thresholds.get("complex", DEFAULT_COMPLEX_THRESHOLD):
            return "complex"
        return "expert"

    def _adjust_thresholds(self) -> None:
        """Self-evolution: adjust complexity thresholds based on per-tier failure rates.

        Uses a rolling window of recent turn outcomes to detect when a tier
        is underperforming (too many failures) or overperforming (could handle
        more complex tasks), and nudges thresholds by small increments.
        """
        thresholds = self._cfg.setdefault("task_complexity_thresholds", {})
        # Ensure defaults exist
        thresholds.setdefault("trivial", DEFAULT_TRIVIAL_THRESHOLD)
        thresholds.setdefault("simple", DEFAULT_SIMPLE_THRESHOLD)
        thresholds.setdefault("complex", DEFAULT_COMPLEX_THRESHOLD)

        # Analyse recent history (last 200 turns) for failure rates per tier
        recent = self._history[-200:]
        if len(recent) < 30:
            return  # not enough data to make statistically meaningful adjustments

        tier_order = ["trivial", "simple", "complex", "expert"]
        failures: Dict[str, int] = {t: 0 for t in tier_order}
        total: Dict[str, int] = {t: 0 for t in tier_order}
        for entry in recent:
            # Determine which tier was originally picked based on complexity
            c = entry["complexity"]
            if c is None:
                continue
            tier = self._tier_for_complexity(c)
            total[tier] += 1
            if entry["failed"]:
                failures[tier] += 1

        # Adjust thresholds for each non-expert tier
        for _i, tier in enumerate(tier_order[:-1]):  # skip expert (no upper tier)
            if total[tier] == 0:
                continue
            failure_rate = failures[tier] / total[tier]
            step = self._cfg.get("self_evolution", {}).get("threshold_step", 0.02)
            ceiling = self._cfg.get("self_evolution", {}).get("threshold_ceiling", 0.9)
            floor = self._cfg.get("self_evolution", {}).get("threshold_floor", 0.1)

            if failure_rate > 0.25:
                # High failure rate → lower the threshold so fewer tasks hit this tier
                new_val = max(thresholds[tier] - step, floor)
                if new_val != thresholds[tier]:
                    logger.info(
                        "router self-evo: lowering %s threshold %.2f→%.2f (failure rate=%.0f%%)",
                        tier, thresholds[tier], new_val, failure_rate * 100,
                    )
                    thresholds[tier] = new_val
            elif failure_rate < 0.03:
                # Low failure rate → raise the threshold so more tasks hit this tier
                new_val = min(thresholds[tier] + step, ceiling)
                if new_val != thresholds[tier]:
                    logger.info(
                        "router self-evo: raising %s threshold %.2f→%.2f (failure rate=%.0f%%)",
                        tier, thresholds[tier], new_val, failure_rate * 100,
                    )
                    thresholds[tier] = new_val

    def _token_budget_for(self, tier: str) -> int:
        return {"trivial": 512, "simple": 1024, "complex": 2048, "expert": 4096}.get(tier, 2048)

    def _build_messages(self, turn: TurnContext) -> List[Dict[str, Any]]:
        """Build compressed prompt: system + recent memories + current input.

        We cap the system message ourselves — the LLM provider then applies
        ``max_tokens`` on the *output* side, which keeps each turn cheap.
        """
        # Auto-detect language for system prompt
        from i18n import get_language
        lang = (get_language() or "zh").lower()

        # Check if OS mode is enabled (coordinator exposes this via ctx.get_plugin)
        os_mode_enabled = False
        if self.ctx is not None:
            coordinator = self.ctx.get_plugin("coordinator")
            if coordinator is not None:
                os_mode_enabled = getattr(coordinator, "_os_mode_enabled", False)

        if lang.startswith("zh"):
            system = (
                "你是 One-Agent，一个极度独立、有思考能力、善用记忆的智能助手。\n\n"
                "【核心铁律 — 你能做的事，永远不要推给用户】\n"
                "1. 自主完成：拿到任务后自己想办法完成，不要求用户补充信息、确认或帮忙。信息不全时，基于常识做最合理的推断，直接动手。\n"
                "2. 多路尝试：如果一种方法失败（工具报错、搜索不可用），立即换另一种方法。比如：搜索不到 → 换关键词再搜；仍无果 → 用自己的知识回答；代码报错 → 换一个实现思路。\n"
                "3. 先做后说：先执行、再汇报。不要把\"要不要我帮你...\"挂在嘴边，直接做了再告诉用户结果。\n"
                "4. 安静兜底：遇到技术故障不要在回复中抱怨或道歉，悄悄换条路继续。只有所有路都走不通时，才告诉用户当前限制并给出最佳替代方案。\n"
                "5. 一次到位：用户要结果，你就给结果。写代码给能跑的完整代码；要信息就把搜索→整理→总结全流程自己跑完，只输出最终答案。\n\n"
                "【记忆系统 — 像人一样记住、会参考、能回忆】\n"
                "每轮对话开始前，系统会在消息里插入一段【相关记忆】（来自长期记忆/知识图谱/嵌入语义检索）。\n"
                "你必须：\n"
                "- 首先逐行阅读这段记忆，找出与当前任务最相关的内容；\n"
                "- 如果记忆里有与用户问题直接相关的信息（如偏好、约定、之前的结论、已采集的数据），优先使用记忆信息，不要重新问或重新查；\n"
                "- 如果记忆与当前问题无关或不足，可以忽略，不要编造；\n"
                "- 在最终答复中，如果用到记忆内容，可以自然引用记忆中的关键词或直接使用，无需注明\"我记得...\"；\n"
                "- 如果用户在对话中更新了信息/偏好/约定，应该在回答中自然吸收并使用，不要前后矛盾。\n\n"
                "【思考流程 — 先想清楚再动手（Chain of Thought）】\n"
                "拿到任务后必须先进行结构化思考并在内部规划以下几步（每一步都要想清楚）：\n"
                "  Step 1. 真正要什么：用一句话提炼用户的核心意图和期望输出形式（代码/回答/方案/列表）；\n"
                "  Step 2. 我已经知道什么：列出我已经有的信息（对话上下文、相关记忆、常识）；\n"
                "  Step 3. 还缺什么：明确哪些信息必须外部获取，哪些可以合理推断；\n"
                "  Step 4. 拆解任务：把任务拆成 3-5 个可执行的小步骤，每一步写清楚做什么；\n"
                "  Step 5. 工具选择：为每个步骤指定一个最合适的工具（如 web_search / calc / system_run / skills 等），写明\"为什么选这个工具\"；\n"
                "  Step 6. 可能风险与兜底：如果某一步失败怎么办，有什么替代方案；\n"
                "  Step 7. 预期最终输出长什么样：心里先预演一遍结果形式（1-2 句描述最终形态）。\n"
                "思考完成后立即按计划执行工具调用，不要把思考过程直接输出给用户。\n\n"
                "【工具使用 — 用足用透】\n"
                "- web_search：有不确定的事实/数据立刻搜；搜不到换关键词再搜；仍无果用自己的知识回答，但要注明\"基于我的知识\"。\n"
                "- calc：遇到数字计算直接用，不要心算。\n"
                "- now：需要当前时间/日期时调用。\n"
                "- system_run：需要在本地执行命令（谨慎、受控）。\n"
                "- skills / document_search / mcp_client / 其他动态加载的 skill：按需调用。\n"
                "工具调用原则：能工具解决就不要问；工具失败就换工具或换关键词；连续失败 3 次再用知识兜底。\n\n"
                "【回复风格】\n"
                "- 直接给答案，不啰嗦铺垫。\n"
                "- 做了再说，不征求意见。\n"
                "- 自然对话，不机器人腔。\n"
                "- 必要时分点说明，长内容给结构。\n\n"
                "【智能路由 — 4 层模型选择】\n"
                "系统使用 4 层智能路由，根据任务复杂度选择模型：\n"
                "  trivial（极简单）→ 轻量模型（快、省）\n"
                "  simple（简单）→ 普通模型\n"
                "  complex（复杂）→ 高级模型\n"
                "  expert（极复杂）→ 专家模型\n"
                "注意：这是按复杂度选模型，不是故障 fallback。模型列表定义在 models/tiers.py 的 MODEL_TIERS 字典，\n"
                "配置在 config/default_config.yaml 的 router 段。添加新模型时，模型名格式为 provider/model（如 nvidia/meta/llama-3.1-70b-instruct），\n"
                "放到对应能力的 tier 列表里，不要在 model 字段重复 provider 前缀。"
            )
            if os_mode_enabled:
                system += (
                    "\n\n【OS 模式已开启 — 你有操作系统权限】\n"
                    "用户已授权你直接操作系统。当任务需要安装软件、下载文件、执行脚本、\n"
                    "修改配置、管理进程等系统操作时，你应该：\n"
                    "1. 自主判断最合适的命令（pip / npm / apt-get / curl / tar 等）；\n"
                    "2. 直接调用 system_run 工具执行，不要问用户「要不要我...」；\n"
                    "3. 遇到依赖缺失时，自动尝试解决（如 pip install 失败就换 pip3，或 apt-get install）；\n"
                    "4. 如果所有工具都不可用，给用户清晰的状态报告和替代建议；\n"
                    "5. 【危险命令黑名单】以下命令绝对不能执行：rm -rf、sudo（除非必要且明确）、\n"
                    "   chmod 777、shutdown/reboot/mkfs/dd/fdisk/iptables。执行前必须告知用户并等确认；\n"
                    "6. 每执行完一个系统命令后，报告执行结果（成功/失败/输出摘要）。"
                )
        else:
            system = (
                "You are One-Agent, an extremely independent, thoughtful, memory-aware AI assistant.\n\n"
                "【Core Rules — Never push to the user what you can handle yourself】\n"
                "1. Self-complete: Figure out how to finish the task yourself. Don't ask for confirmations, more info, or help. If info is missing, make the most reasonable assumption and just do it.\n"
                "2. Multi-path: If one approach fails, immediately switch to another. Search down? retry different keywords. Code broken? try a different implementation.\n"
                "3. Act first, report after: Execute, then tell the user what you did. Never ask \"should I...\" — just do it and share the result.\n"
                "4. Silent fallback: Don't complain or apologize about technical issues. Quietly find another way. Only when all paths are exhausted, state the limitation and offer the best alternative.\n"
                "5. One-shot delivery: Give complete, runnable results. For info tasks, run the full search→analyze→summarize pipeline yourself, output only the final answer.\n\n"
                "【Memory System — Remember, reference, recall】\n"
                "Before every turn, a block of [Relevant Memory] is injected into the conversation (from long-term memory / knowledge graph / embedding-based semantic retrieval). You MUST:\n"
                "- Read memory first — scan every line to find what relates to the current task;\n"
                "- Prefer memory over re-asking — if memory already holds the answer (user preferences, past agreements, previously collected data), USE IT. Don't ask the same question twice.\n"
                "- Ignore when irrelevant — if memory doesn't match, ignore it. Never fabricate memory contents.\n"
                "- Absorb updates — when the user updates preferences / agreements / facts, absorb them into your reasoning so later answers stay consistent.\n"
                "You don't need to prefix replies with \"I remember...\". Just naturally use what you recalled.\n\n"
                "[Thinking — Plan before you act (Chain of Thought)]\n"
                "For every task, you MUST perform structured thinking internally. Walk through these steps:\n"
                "  Step 1. What does the user actually want? one sentence for intent + output form (code / answer / plan / list).\n"
                "  Step 2. What do I already know? list context from conversation, memory hits, and common sense.\n"
                "  Step 3. What am I still missing? what must come from outside, what can I infer reasonably.\n"
                "  Step 4. Break it down — 3-5 concrete, executable sub-steps, each described clearly.\n"
                "  Step 5. Pick tools — assign the BEST tool per sub-step (web_search / calc / system_run / skill ...) and state why.\n"
                "  Step 6. Risks & fallbacks — what if a step fails? what is plan B.\n"
                "  Step 7. Envision the final output — 1-2 sentences describing what the final result looks like.\n"
                "Once the plan is clear, execute the tool calls. Do NOT dump the thinking to the user.\n\n"
                "【Tools — Use them thoroughly】\n"
                "- web_search: search any uncertain facts/data immediately; retry with different keywords; fall back to your knowledge when search fails (and say so).\n"
                "- calc: for any math — never mental calculate.\n"
                "- now: when current time / date is needed.\n"
                "- system_run: to run controlled local commands.\n"
                "- skills / document_search / mcp_client / other dynamically loaded skills: call as needed.\n"
                "Tool principle: use tools rather than ask; retry on failure with different tools/keywords; fall back to knowledge after 3 consecutive tool failures.\n\n"
                "【Style】\n"
                "- Direct answers, no fluff.\n"
                "- Act first, ask never.\n"
                "- Natural, not robotic.\n"
                "- Bullet points for long content; structural output for long answers.\n\n"
                "【Smart Router — 4-Layer Model Selection】\n"
                "The system uses a 4-layer smart router that selects models based on task complexity:\n"
                "  trivial → lightweight model (fast, cheap)\n"
                "  simple → standard model\n"
                "  complex → advanced model\n"
                "  expert → strongest model\n"
                "Note: This is complexity-based selection, NOT failover fallback. Model lists are in models/tiers.py (MODEL_TIERS dict),\n"
                "config in config/default_config.yaml (router section). When adding models, use provider/model format (e.g. nvidia/meta/llama-3.1-70b-instruct),\n"
                "place in the appropriate tier list, and do NOT duplicate the provider prefix in the model field."
            )
            if os_mode_enabled:
                system += (
                    "\n\n【OS Mode ENABLED — You have OS operation permissions】\n"
                    "The user has authorized you to directly operate the system. When a task requires\n"
                    "installing software, downloading files, running scripts, modifying configs, or managing\n"
                    "processes, you should:\n"
                    "1. Autonomously decide the best command (pip / npm / apt-get / curl / tar / etc.);\n"
                    "2. Directly call system_run to execute — do NOT ask \"should I...\";\n"
                    "3. When dependencies are missing, automatically try solutions (e.g. pip fails → try pip3, or apt-get);\n"
                    "4. Report execution results clearly (success/failure/output summary);\n"
                    "5. 【Dangerous command BLACKLIST】NEVER execute: rm -rf, sudo (unless necessary & explicit), chmod 777,\n"
                    "   shutdown/reboot/mkfs/dd/fdisk/iptables. Tell the user and wait for confirmation before these;\n"
                    "6. After each system command, report the result."
                )
        history = self._history_tail(turn.session_id)
        # Smart compression: keep recent turns + important context
        compression_cfg = self._cfg.get("context_compression", {}) or {}
        if compression_cfg.get("enabled", True) and len(history) > 6:
            # Keep last 6 turns, but try to preserve important context
            # by summarizing older turns if available
            recent_history = history[-6:]

            # If we have a summary capability, use it
            if compression_cfg.get("summarize_old_turns", False) and len(history) > 6:
                old_turns = history[:-6]
                # Create a summary of old turns — use reply (assistant output)
                # rather than input (user message) for meaningful context
                summary_parts = []
                for h in old_turns:
                    if h.get("reply"):
                        summary_parts.append(f"Earlier reply: {str(h['reply'])[:80]}...")

                # Always preserve the system prompt — it contains core rules
                messages = [{"role": "system", "content": system}]
                if summary_parts:
                    summary_msg = {
                        "role": "system",
                        "content": "Previous context summary:\n" + "\n".join(summary_parts[-3:])
                    }
                    messages.append(summary_msg)
            else:
                messages = [{"role": "system", "content": system}]

            history = recent_history
        else:
            messages = [{"role": "system", "content": system}]

        for h in history:
            messages.append({"role": "user", "content": h["input"]})
            if h["reply"] is not None:
                messages.append({"role": "assistant", "content": h["reply"]})
        messages.append({"role": "user", "content": turn.input_text})
        return messages

    def _history_tail(self, session_id: str) -> List[Dict[str, Any]]:
        # cheap in-memory per-session history — the memory plugin provides
        # long-term cross-session recall via a separate event.
        if not hasattr(self, "_session_history"):
            self._session_history: Dict[str, List[Dict[str, Any]]] = {}
        return self._session_history.get(session_id, [])


# NOTE: ``HistoryRecorder`` was removed in v2.1 — it duplicated
# ``SmartRouter._session_history`` and caused two writes per turn.
# SmartRouter is now the single source of per-session history.
