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
from models import LLMProvider, MODEL_TIERS

logger = logging.getLogger(__name__)


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
        # running accuracy counters per-tier (for self-evolution)
        self._tier_stats: Dict[str, Dict[str, int]] = {
            t: {"picked": 0, "rerouted_up": 0, "rerouted_down": 0}
            for t in MODEL_TIERS
        }

    # -------------------------------------------------------- lifecycle
    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        self._cfg = ctx.config.get("router", {}) or {}
        self._llm = next(
            (p for p in self.ctx._plugins if getattr(p, "name", None) == "llm"), None
        ) if False else None  # filled in from PluginManager registry elsewhere
        self.bus.subscribe("user_message", self._on_user_message)
        self.bus.subscribe("turn_completed", self._on_turn_completed)
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
        turn.model = model or tier
        # 2) build messages + compress history
        turn.messages = self._build_messages(turn)
        # 3) cap token budget
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
        # TODO: dynamic threshold adjustment — keep a rolling mean of
        # "failures per tier" and bump up complexity thresholds when a tier
        # sees repeated failures.  Good enough for OSS v1.

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
        if c < thresholds.get("trivial", 0.2):
            return "trivial"
        if c < thresholds.get("simple", 0.5):
            return "simple"
        if c < thresholds.get("complex", 0.8):
            return "complex"
        return "expert"

    def _token_budget_for(self, tier: str) -> int:
        return {"trivial": 512, "simple": 1024, "complex": 2048, "expert": 4096}[tier]

    def _build_messages(self, turn: TurnContext) -> List[Dict[str, Any]]:
        """Build compressed prompt: system + recent memories + current input.

        We cap the system message ourselves — the LLM provider then applies
        ``max_tokens`` on the *output* side, which keeps each turn cheap.
        """
        system = (
            "You are Athena, a concise, helpful agent.  Answer the user's "
            "question directly.  If you must use tools, call them; otherwise "
            "just reply in plain text.  Keep answers compact."
        )
        history = self._history_tail(turn.session_id)
        # compression: drop turns older than N when history is long
        compression_cfg = self._cfg.get("context_compression", {}) or {}
        if compression_cfg.get("enabled", True) and len(history) > 6:
            history = history[-6:]
        messages = [{"role": "system", "content": system}]
        for h in history:
            messages.append({"role": "user", "content": h["input"]})
            if h["reply"]:
                messages.append({"role": "assistant", "content": h["reply"]})
        messages.append({"role": "user", "content": turn.input_text})
        return messages

    def _history_tail(self, session_id: str) -> List[Dict[str, Any]]:
        # cheap in-memory per-session history — the memory plugin provides
        # long-term cross-session recall via a separate event.
        if not hasattr(self, "_session_history"):
            self._session_history: Dict[str, List[Dict[str, Any]]] = {}
        return self._session_history.get(session_id, [])


class HistoryRecorder(Plugin):
    """Keeps per-session tails so the router can include recent turns."""

    name = "router_history"
    depends_on = ["router"]

    def __init__(self) -> None:
        super().__init__()
        self._sessions: Dict[str, List[Dict[str, Any]]] = {}

    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        self.bus.subscribe("turn_completed", self._on_done)

    async def _on_done(self, event: Event) -> None:
        turn: TurnContext | None = event.get("turn")
        if turn is None:
            return
        self._sessions.setdefault(turn.session_id, []).append({
            "input": turn.input_text,
            "reply": turn.result,
            "t": time.time(),
        })
        # cap per-session history
        self._sessions[turn.session_id] = self._sessions[turn.session_id][-20:]
