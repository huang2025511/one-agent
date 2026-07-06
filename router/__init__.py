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
import time
from typing import Any, Dict, List

from core.context import TurnContext
from core.events import Event
from core.plugin import Plugin
from models import LLMProvider
from models.tiers import MODEL_TIERS
from utils.intent_classifier import get_classifier

logger = logging.getLogger(__name__)

# Default complexity thresholds for task classification
DEFAULT_TRIVIAL_THRESHOLD = 0.2
DEFAULT_SIMPLE_THRESHOLD = 0.5
DEFAULT_COMPLEX_THRESHOLD = 0.8
MAX_COMPLEXITY = 100


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
        # 1) classify complexity — LLM-based with heuristic fast-path
        turn.estimated_complexity, intent_meta = await self._classify_smart(turn.input_text)
        tier = self._tier_for_complexity(turn.estimated_complexity)
        turn.meta.update(intent_meta)
        # 2) build messages + compress history
        turn.messages = self._build_messages(turn)
        # 3) pass context_compression config to coordinator via turn.meta
        compression_cfg = self._cfg.get("context_compression", {}) or {}
        turn.meta["context_compression"] = compression_cfg.get("enabled", True)
        # 4) token-aware 自适应升 tier：长上下文任务自动升级模型
        # 长对话历史即使问题简单，也需要更强的模型来处理和综合信息。
        # 估算消息总 token 数，超过阈值则升一层（最多到 expert）。
        token_cfg = (self._cfg.get("token_aware_routing", {}) or {})
        if token_cfg.get("enabled", True):
            estimated_tokens = self._estimate_tokens(turn.messages)
            up_threshold = token_cfg.get("upgrade_threshold", 3000)
            if estimated_tokens >= up_threshold and tier != "expert":
                old_tier = tier
                tier_order = ["trivial", "simple", "complex", "expert"]
                idx = tier_order.index(tier) if tier in tier_order else 1
                # 每超阈值一倍再升一层（最多到 expert）
                steps = min(
                    len(tier_order) - 1 - idx,
                    max(1, int(estimated_tokens / up_threshold)),
                )
                tier = tier_order[idx + steps]
                turn.meta["token_upgraded_from"] = old_tier
                turn.meta["estimated_input_tokens"] = estimated_tokens
                logger.info(
                    "router: token-aware upgrade %s → %s (estimated %d tokens)",
                    old_tier, tier, estimated_tokens,
                )
        # 5) 选模型 + cap token budget
        model = self._llm.model_for_tier(tier) if self._llm else None
        # 5.1) 工具支持检测：如果任务需要工具但模型不支持，自动升级 tier
        needs_tools = intent_meta.get("needs_tools", False)
        if needs_tools and model and self._llm and not self._llm.model_supports_tools(model):
            old_tier = tier
            old_model = model
            tier_order = ["trivial", "simple", "complex", "expert"]
            idx = tier_order.index(tier) if tier in tier_order else 1
            # 逐级升级，遍历该 tier 所有模型，找到第一个支持工具的
            from models.tiers import MODEL_TIERS
            upgraded = False
            for new_idx in range(idx + 1, len(tier_order)):
                new_tier = tier_order[new_idx]
                # 先尝试 model_for_tier 选的模型
                candidate = self._llm.model_for_tier(new_tier)
                if candidate and self._llm.model_supports_tools(candidate):
                    tier = new_tier
                    model = candidate
                    upgraded = True
                    break
                # 再遍历该 tier 所有模型
                for alt_model in MODEL_TIERS.get(new_tier, []):
                    if self._llm.model_supports_tools(alt_model):
                        provider = alt_model.split("/")[0] if "/" in alt_model else ""
                        if provider and self._llm._has_usable_key(provider):
                            tier = new_tier
                            model = alt_model
                            upgraded = True
                            break
                if upgraded:
                    break
            if upgraded:
                turn.meta["tool_support_upgraded_from"] = old_tier
                logger.info(
                    "router: tool-support upgrade %s→%s, model %s→%s",
                    old_tier, tier, old_model, model,
                )
            else:
                logger.warning(
                    "router: no tool-capable model available for '%s', using %s (text-only mode)",
                    turn.input_text[:40], model,
                )
                turn.meta["no_tool_model"] = True
        turn.model = model  # None is fine — chat_completion falls back to default model
        turn.token_budget = self._token_budget_for(tier)
        # 6) 检测系统操作需求，自动标记需要 OS 模式
        if intent_meta.get("needs_system", False):
            turn.meta["needs_system_access"] = True
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

    # --- LLM-based intent classifier ---
    # Cache: input_hash → (complexity, meta, timestamp)
    _intent_cache: Dict[str, tuple] = {}
    _INTENT_CACHE_TTL = 3600  # 1 hour
    _INTENT_CACHE_MAX = 500

    async def _classify_smart(self, text: str) -> tuple:
        """LLM-based intent classification with heuristic fast-path.

        Returns (complexity: float, meta: dict) where meta may contain:
        - needs_tools: bool
        - needs_system: bool
        - task_type: str (chat/design/code/analysis/action/system)
        - intent_source: 'fast_path' | 'llm' | 'fallback'

        Strategy:
        1. Fast-path: short messages (<15 chars) that look like greetings → 0.05
        2. LLM classification: ask a cheap model to return JSON
        3. Fallback: if LLM fails, use heuristic _classify()
        """
        t = text.strip()
        if not t:
            return 0.0, {"intent_source": "empty"}

        # --- Fast path: ultra-short messages ---
        if len(t) <= 15 and not any(c in t for c in "？?！!。.，,\n"):
            # Greetings, acknowledgements — skip LLM entirely
            greetings = ("你好", "嗨", "hi", "hello", "hey", "谢谢", "thanks",
                         "ok", "好的", "嗯", "再见", "bye", "哈喽", "在吗")
            if t.lower() in greetings or any(t.startswith(g) for g in greetings):
                return 0.05, {
                    "needs_tools": False,
                    "needs_system": False,
                    "task_type": "chat",
                    "intent_source": "fast_path",
                }

        # --- LLM classification ---
        if self._llm is None:
            return self._classify_heuristic(t), {"intent_source": "fallback"}

        # Check cache
        import hashlib
        cache_key = hashlib.md5(t.encode()).hexdigest()
        cached = self._intent_cache.get(cache_key)
        if cached:
            complexity, meta, ts = cached
            if time.time() - ts < self._INTENT_CACHE_TTL:
                meta = {**meta, "intent_source": "cache"}
                return complexity, meta

        # Call LLM for classification
        try:
            complexity, meta = await self._llm_classify_intent(t)
        except Exception:
            logger.exception("LLM intent classification failed, falling back to heuristic")
            complexity = self._classify_heuristic(t)
            meta = {"needs_tools": False, "needs_system": False,
                    "task_type": "unknown", "intent_source": "fallback"}

        # Cache result
        if len(self._intent_cache) >= self._INTENT_CACHE_MAX:
            # Evict oldest entry
            oldest = min(self._intent_cache, key=lambda k: self._intent_cache[k][2])
            del self._intent_cache[oldest]
        self._intent_cache[cache_key] = (complexity, meta, time.time())

        return complexity, meta

    async def _llm_classify_intent(self, text: str) -> tuple:
        """Use a cheap LLM to classify user intent.

        Returns (complexity: float, meta: dict).
        """
        prompt = (
            "分析用户输入的意图，返回JSON格式（不要其他内容）。\n"
            "字段说明：\n"
            '- complexity: 0.0-1.0 复杂度（0=闲聊, 0.3=简单问答, 0.5=需要思考/设计/分析, 0.8=专家级复杂任务）\n'
            '- needs_tools: 是否需要调用工具（搜索/计算/执行命令/读写文件）\n'
            '- needs_system: 是否需要操作系统（git/文件/命令行/服务器）\n'
            '- task_type: chat/design/code/analysis/action/system\n\n'
            f"用户输入：{text[:500]}\n\n"
            '只返回JSON，示例：{"complexity": 0.7, "needs_tools": true, "needs_system": false, "task_type": "design"}'
        )

        result = await self._llm.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model=None,  # use default (cheapest available)
            temperature=0.0,
            max_tokens=256,  # 80 太小，嵌套/带说明的 JSON 容易被截断
            tools=None,
            use_cache=True,
        )

        raw = (result.get("text") or "").strip()
        # Parse JSON from response
        import json as _json
        import re as _re

        data: dict = {}
        try:
            # Try to extract JSON from the response (model may wrap in markdown)
            json_str = raw
            if "```" in raw:
                # Extract from code block — 贪婪匹配，避免嵌套 JSON 被截断
                m = _re.search(r'```(?:json)?\s*(\{.*\})\s*```', raw, _re.DOTALL)
                if m:
                    json_str = m.group(1)
            # Find first { ... last } if surrounded by other text
            if not json_str.startswith("{"):
                start = json_str.find("{")
                end = json_str.rfind("}")
                if start >= 0 and end > start:
                    json_str = json_str[start:end + 1]

            data = _json.loads(json_str)
        except (_json.JSONDecodeError, ValueError) as e:
            logger.warning(
                "router: LLM 意图 JSON 解析失败 (%s)，回退到启发式。raw=%s",
                e, raw[:200],
            )
            # 解析失败：回退到启发式分类，不让一次坏响应拖垮整个 turn
            complexity = self._classify_heuristic(text)
            return complexity, {
                "needs_tools": False,
                "needs_system": False,
                "task_type": "unknown",
                "intent_source": "fallback",
            }

        try:
            complexity = float(data.get("complexity", 0.3))
        except (TypeError, ValueError):
            complexity = 0.3
        complexity = max(0.0, min(1.0, complexity))

        meta = {
            "needs_tools": bool(data.get("needs_tools", False)),
            "needs_system": bool(data.get("needs_system", False)),
            "task_type": str(data.get("task_type", "unknown")),
            "intent_source": "llm",
        }
        return complexity, meta

    def _classify_heuristic(self, text: str) -> float:
        """Heuristic fallback — used when LLM is unavailable.

        Delegates to the unified IntentClassifier's fallback heuristic.
        """
        classifier = get_classifier(self._llm)
        complexity, _meta = classifier.classify_complexity(text)
        return complexity

    # Backward-compatible alias for tests
    _classify = _classify_heuristic

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

    def _estimate_tokens(self, messages: List[Dict[str, Any]]) -> int:
        """估算消息列表的 token 总数。

        优先用 tiktoken（如果安装了），否则用中文/非中文混合估算。
        中文 * 0.6 + 非中文字符 // 4，与 coordinator 的 _trim_messages 保持一致。
        """
        # 先试 tiktoken（更准确）
        try:
            import tiktoken  # type: ignore
            enc = tiktoken.get_encoding("cl100k_base")
            total = 0
            for msg in messages:
                content = str(msg.get("content", ""))
                total += len(enc.encode(content))
                # 每条消息 +4 tokens（role + 分隔符，GPT 风格估算）
                total += 4
            return total
        except ImportError:
            pass
        # fallback：中文/非中文加权估算
        total = 0
        for msg in messages:
            content = str(msg.get("content", ""))
            chinese = sum(1 for c in content if "\u4e00" <= c <= "\u9fff")
            non_chinese = len(content) - chinese
            total += int(chinese * 0.6 + non_chinese / 4)
            total += 4  # role + separator overhead
        return total

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
                "【工具使用 — 必须动手，不能嘴炮】\n"
                "你是一个有动手能力的智能助手，遇到以下情况必须调用工具，绝对不能靠脑子空想：\n"
                "- web_search：任何事实性问题、最新信息、不确定的数据，立刻搜索！不要凭记忆回答。\n"
                "- python_execute：遇到数学计算、数据处理、代码验证、公式求解，直接写代码执行！不要心算或猜测。\n"
                "- calc：简单计算可以用这个工具。\n"
                "- now：需要当前时间/日期时调用。\n"
                "- system_run：需要在本地执行命令（谨慎、受控）。\n"
                "- skills / document_search / mcp_client / 其他动态加载的 skill：按需调用。\n\n"
                "工具调用铁律：\n"
                "1. 能工具解决的，绝对不要自己回答；\n"
                "2. 工具失败就换工具或换关键词，不要放弃；\n"
                "3. 连续失败 3 次再用知识兜底，但要明确注明\"基于我的知识\"；\n"
                "4. 调用工具后，必须根据工具返回的结果给出最终答案，不能忽略工具结果。\n\n"
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
        """Return recent messages for a session.

        First checks in-memory cache for speed, then falls back to
        persistent session store (SQLite) to survive restarts.
        Returns format: [{"input": "...", "reply": "..."}, ...]
        """
        if not hasattr(self, "_session_history"):
            self._session_history: Dict[str, List[Dict[str, Any]]] = {}

        # First check in-memory cache
        if session_id in self._session_history:
            return self._session_history[session_id]

        # Fall back to persistent session store
        session_store = getattr(self.ctx, "session_store", None)
        if session_store is not None:
            try:
                session_data = session_store.get_session(session_id)
                if session_data and "messages" in session_data:
                    # Convert to the format expected by _build_messages
                    history = []
                    i = 0
                    while i < len(session_data["messages"]):
                        msg = session_data["messages"][i]
                        if msg.get("role") == "user":
                            # Look for the next assistant reply
                            j = i + 1
                            reply = ""
                            while j < len(session_data["messages"]):
                                next_msg = session_data["messages"][j]
                                if next_msg.get("role") == "assistant":
                                    reply = next_msg.get("content", "")
                                    break
                                j += 1
                            history.append({
                                "input": msg.get("content", ""),
                                "reply": reply,
                            })
                            i = j + 1
                        else:
                            i += 1
                    # Cache in memory for next time
                    self._session_history[session_id] = history
                    return history
            except Exception:
                logger.exception("_history_tail: failed to load from session_store")

        return []


# NOTE: ``HistoryRecorder`` was removed in v2.1 — it duplicated
# ``SmartRouter._session_history`` and caused two writes per turn.
# SmartRouter is now the single source of per-session history.
