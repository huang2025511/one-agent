"""Model catalog: auto-discover, classify, and tier LLM models.

When you give it a base URL and API key, the catalog fetches the
provider's full model list, classifies each model by its constraints
(free/paid, context window, modalities, rate limits, features), and
exposes filtering and interactive selection helpers.

OpenAI-compatible providers expose ``GET /v1/models``.  Many of them
(OpenRouter, SenseNova, etc.) include rich metadata in the response —
pricing, context length, modalities, supported features.  This module
normalises those fields and falls back to heuristic inference when
metadata is missing.

Smart tier assignment (new)
---------------------------

``auto_classify_tier(model)`` puts a model into one of:

  * ``trivial``  — free, small context, text-only, no reasoning/tools
  * ``simple``   — free OR small/medium context, fast / cheap
  * ``complex``  — paid, large context, multimodal / has tools
  * ``expert``   — paid, huge context OR has reasoning OR name hints
                   (opus, o1/o3, ultra, max, pro, sonnet-4...)

``rebuild_tiers()`` re-populates ``MODEL_TIERS`` from a live provider
catalog so adding a new model automatically gets it slotted into the
right tier.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


# ============================================================
# ModelInfo
# ============================================================
@dataclass
class ModelInfo:
    """Normalised metadata for a single LLM model."""

    id: str                                  # model identifier the API expects
    name: str = ""                           # human-readable name
    provider: str = ""                       # "openai", "sensenova", ...
    description: str = ""
    created: int = 0
    context_length: int = 0                  # max input tokens
    max_output_length: int = 0
    input_modalities: List[str] = field(default_factory=list)   # text/image/audio
    output_modalities: List[str] = field(default_factory=list)
    pricing: Dict[str, float] = field(default_factory=dict)     # per-token USD
    features: List[str] = field(default_factory=list)           # tools, json_mode, ...
    quantization: str = ""
    is_free: bool = False
    tags: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)
    # Populated by auto_classify_tier()
    tier: str = ""
    # Populated by detect_capabilities() — what the model can do
    # (text / vision / image_generation / video / audio_in / audio_out /
    #  embeddings / code / tools / reasoning / long_context / etc.)
    capabilities: "frozenset[str]" = field(default_factory=frozenset, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        # capabilities is a frozenset — make it JSON-friendly
        if "capabilities" in d and d["capabilities"] is not None:
            d["capabilities"] = sorted(d["capabilities"])
        return d


# Tokens per second-rate (rough heuristic by tier)
_RATE_BUDGETS = {
    "free": (20, 200_000),
    "trial": (60, 1_000_000),
    "standard": (600, 10_000_000),
    "premium": (2000, 100_000_000),
}


def _classify_free(pricing: Dict[str, float]) -> bool:
    """Return True when every pricing field is 0 / empty / unparseable."""
    if not pricing:
        return False
    for v in pricing.values():
        try:
            if float(v) > 0:
                return False
        except (TypeError, ValueError):
            continue
    return True


# ============================================================
# ModelCatalog
# ============================================================
class ModelCatalog:
    """Auto-discover and classify models from any OpenAI-compatible API.

    Usage::

        cat = ModelCatalog(base_url="https://token.sensenova.cn/v1",
                           api_key="sk-...")
        await cat.refresh()
        for m in cat.filter(free_only=True):
            print(m.id, m.tier, m.context_length)
    """

    # Cache TTL (seconds)
    DEFAULT_TTL = 3600

    def __init__(self, base_url: str, api_key: str = "",
                 provider: str = "", ttl: int = DEFAULT_TTL,
                 client: Optional[httpx.AsyncClient] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.provider = provider
        self.ttl = ttl
        self._models: Dict[str, ModelInfo] = {}
        self._fetched_at: float = 0.0
        self._own_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30)

    async def aclose(self) -> None:
        if not self._own_client:
            return
        try:
            if self._client is not None:
                await self._client.aclose()
        except Exception:
            pass

    # ---------------------------------------------------------- refresh
    async def refresh(self, force: bool = False) -> int:
        """Fetch the model list from the provider, classify each entry,
        and assign a tier.  Returns the number of models loaded."""
        import time as _t
        now = _t.time()
        if not force and self._models and (now - self._fetched_at) < self.ttl:
            return len(self._models)

        url = f"{self.base_url}/models"
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # Add explicit timeout and retry logic
        max_retries = 3
        retry_delay = 1.0

        for attempt in range(max_retries):
            try:
                resp = await asyncio.wait_for(
                    self._client.get(url, headers=headers),
                    timeout=30.0
                )
                break
            except asyncio.TimeoutError:
                logger.warning("catalog refresh GET %s timeout (attempt %d/%d)",
                             url, attempt + 1, max_retries)
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    logger.error("catalog refresh failed after %d attempts", max_retries)
                    return 0
            except httpx.HTTPError as exc:
                logger.warning("catalog refresh GET %s failed: %s (attempt %d/%d)",
                             url, exc, attempt + 1, max_retries)
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    logger.error("catalog refresh failed after %d attempts", max_retries)
                    return 0

        if resp.status_code >= 400:
            logger.warning("catalog refresh %s → HTTP %d", url, resp.status_code)
            return 0
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("catalog refresh: invalid JSON: %s", exc)
            return 0
        items = data.get("data") or data.get("models") or []
        if not isinstance(items, list):
            return 0

        self._models = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id") or item.get("name") or "").strip()
            if not mid:
                continue
            info = self._normalize(item, model_id=mid)
            info.tier = auto_classify_tier(info)
            self._models[mid] = info
        self._fetched_at = now
        return len(self._models)

    def _normalize(self, item: Dict[str, Any], model_id: str) -> ModelInfo:
        """Normalise a single API response entry into ModelInfo."""
        # Pricing may be a dict or a single number — accept both
        pricing_raw = item.get("pricing") or {}
        pricing: Dict[str, float] = {}
        if isinstance(pricing_raw, dict):
            for k, v in pricing_raw.items():
                try:
                    pricing[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
        elif isinstance(pricing_raw, (int, float)):
            pricing["prompt"] = float(pricing_raw)

        # is_free 优先取 API 显式声明；仅当 API 未声明时再用 pricing 推断。
        # 之前的 `bool(item.get("is_free") or _classify_free(pricing))` 写法
        # 会让 API 明确声明 is_free=False 的付费模型被 pricing 推断覆盖
        # （因为 False or X 等价于 X）。
        raw_is_free = item.get("is_free")
        if raw_is_free is None:
            is_free = _classify_free(pricing)
        else:
            is_free = bool(raw_is_free)

        ctx = int(item.get("context_length") or item.get("max_context") or 0)
        max_out = int(item.get("max_output_length") or item.get("max_tokens") or 0)

        in_mod = list(item.get("input_modalities") or item.get("modalities") or [])
        out_mod = list(item.get("output_modalities") or [])
        if not in_mod:
            in_mod = ["text"] if "vision" not in (item.get("supported_features") or []) else ["text", "image"]
        if not out_mod:
            out_mod = ["text"]

        features = list(item.get("supported_features") or item.get("features") or [])
        if not features:
            if "vision" in str(item).lower() or any("image" in m for m in in_mod):
                features.append("vision")
            if "tools" in str(item).lower() or item.get("tool_support") == "yes":
                features.append("tools")

        info = ModelInfo(
            id=model_id,
            name=str(item.get("name") or model_id),
            provider=self.provider,
            description=str(item.get("description") or ""),
            created=int(item.get("created") or 0),
            context_length=ctx,
            max_output_length=max_out,
            input_modalities=in_mod,
            output_modalities=out_mod,
            pricing=pricing,
            features=features,
            quantization=str(item.get("quantization") or ""),
            is_free=is_free,
            tags=self._derive_tags(ctx, is_free, features, in_mod),
            raw=item,
        )
        # Detect what the model can do (vision / video / tools / ...) and
        # store as a frozenset on the ModelInfo so ``recommend()`` and
        # CLI listings can filter by capability without re-scanning names.
        try:
            from .capabilities import detect_capabilities
            info.capabilities = frozenset(detect_capabilities(info))
        except Exception:  # noqa: BLE001
            info.capabilities = frozenset()
        return info

    @staticmethod
    def _derive_tags(ctx: int, is_free: bool, features: List[str],
                     in_mod: List[str]) -> List[str]:
        tags: List[str] = []
        if is_free:
            tags.append("free")
        else:
            tags.append("paid")
        if ctx <= 0:
            tags.append("unknown-context")
        elif ctx < 8_000:
            tags.append("tiny-context")
        elif ctx < 32_000:
            tags.append("small-context")
        elif ctx < 128_000:
            tags.append("medium-context")
        elif ctx < 200_000:
            tags.append("large-context")
        else:
            tags.append("huge-context")
        if "image" in in_mod or "vision" in features:
            tags.append("vision")
        if "tools" in features:
            tags.append("tools")
        if "reasoning" in features or "thinking" in features:
            tags.append("reasoning")
        if not any(m != "text" for m in in_mod):
            tags.append("text-only")
        return tags

    # ---------------------------------------------------------- accessors
    def all(self) -> List[ModelInfo]:
        return list(self._models.values())

    def get(self, model_id: str) -> Optional[ModelInfo]:
        return self._models.get(model_id)

    # ---------------------------------------------------------- filtering
    def filter(
        self,
        free_only: bool = False,
        paid_only: bool = False,
        min_context: int = 0,
        input_modality: Optional[str] = None,
        feature: Optional[str] = None,
        tag: Optional[str] = None,
        tier: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> List[ModelInfo]:
        items = self.all()
        if free_only:
            items = [m for m in items if m.is_free]
        if paid_only:
            items = [m for m in items if not m.is_free]
        if min_context > 0:
            items = [m for m in items if m.context_length >= min_context]
        if input_modality:
            items = [m for m in items if input_modality in m.input_modalities]
        if feature:
            items = [m for m in items if feature in m.features]
        if tag:
            items = [m for m in items if tag in m.tags]
        if tier:
            items = [m for m in items if m.tier == tier]
        if keyword:
            kw = keyword.lower()
            items = [m for m in items if kw in m.id.lower() or kw in m.name.lower()]
        return items

    # ---------------------------------------------------------- intent
    def classify_intent(self, text: str) -> Dict[str, Any]:
        """Pull a filter spec out of free-form text.

        Examples::

            "free models"           → {"free_only": True}
            "200k context"          → {"min_context": 200_000}
            "vision models"         → {"input_modality": "image"}
            "reasoning models"      → {"feature": "reasoning"}
            "free + 100k context"   → {"free_only": True, "min_context": 100_000}
            "expert tier"           → {"tier": "expert"}
        """
        spec: Dict[str, Any] = {}
        t = (text or "").lower()
        if any(k in t for k in ("free", "免费", "试用")):
            spec["free_only"] = True
        if any(k in t for k in ("paid", "收费", "付费")):
            spec["paid_only"] = True
        m = re.search(r"(\d+)\s*k\b", t)
        if m:
            spec["min_context"] = int(m.group(1)) * 1000
        if any(k in t for k in ("vision", "视觉", "image", "图像", "多模态")):
            spec["input_modality"] = "image"
        if any(k in t for k in ("reasoning", "推理", "思考", "thinking")):
            spec["feature"] = "reasoning"
        if any(k in t for k in ("tool", "工具", "function")):
            spec["feature"] = spec.get("feature") or "tools"
        for tier in ("trivial", "simple", "complex", "expert"):
            if tier in t or f"{tier} tier" in t or f"{tier}层" in t:
                spec["tier"] = tier
                break
        return spec

    # ---------------------------------------------------------- describe
    def describe(self, model_id: str) -> str:
        info = self.get(model_id)
        if not info:
            return f"(no such model: {model_id})"
        lines = [
            f"Model:        {info.id}",
            f"Provider:     {info.provider or '(unknown)'}",
            f"Name:         {info.name}",
        ]
        if info.description:
            lines.append(f"Description:  {info.description}")
        lines += [
            f"Context:      {info.context_length:,} tokens" if info.context_length else "Context:      (unknown)",
            f"Max output:   {info.max_output_length:,} tokens" if info.max_output_length else "Max output:   (unknown)",
            f"Modalities:   in={','.join(info.input_modalities) or '?'}  out={','.join(info.output_modalities) or '?'}",
            f"Features:     {','.join(info.features) or '(none)'}",
            f"Free:         {'YES' if info.is_free else 'NO'}",
            f"Quantization: {info.quantization or '(n/a)'}",
            f"Tier:         {info.tier or auto_classify_tier(info)}",
        ]
        if info.pricing:
            pretty = ", ".join(f"{k}={v}" for k, v in info.pricing.items())
            lines.append(f"Pricing:      {pretty}")
        # Rate-limit hint
        if info.is_free:
            rpm, tpm = _RATE_BUDGETS["free"]
        elif "trial" in info.tags or "free" in info.tags:
            rpm, tpm = _RATE_BUDGETS["trial"]
        elif info.context_length >= 200_000:
            rpm, tpm = _RATE_BUDGETS["premium"]
        else:
            rpm, tpm = _RATE_BUDGETS["standard"]
        lines.append(f"Rate-limit:   ~{rpm} req/min  /  ~{tpm:,} tok/min (heuristic)")
        return "\n".join(lines)

    # ---------------------------------------------------------- recommend
    def recommend(self) -> Dict[str, Optional[ModelInfo]]:
        """Pick the best model for each common use-case.

        Returns a dict keyed by category (``best_paid``, ``best_free``,
        ``best_for_text``, ``best_for_vision``, ``best_for_image``,
        ``best_for_video``, ``best_for_audio``, ``best_for_code``,
        ``best_for_agent``, ``best_for_reasoning``,
        ``best_for_long_context``, ``best_for_embeddings``).  Categories
        with no qualifying model get ``None``.

        Re-runs ``detect_capabilities()`` for every model so a freshly-
        added model is reflected immediately — this is cheap (one regex
        sweep per model) and avoids stale data when the catalog was
        loaded before a new feature flag was added.
        """
        from .capabilities import detect_capabilities
        from .capabilities import recommend as _recommend
        # Refresh capabilities — cheap and idempotent
        for m in self._models.values():
            try:
                m.capabilities = frozenset(detect_capabilities(m))
            except Exception:  # noqa: BLE001
                m.capabilities = frozenset()
        return _recommend(self._models.values())


# ============================================================
# Smart tier classification  (the new feature)
# ============================================================

# Regex of "obviously expert" name fragments
_EXPERT_HINTS = re.compile(
    r"(opus|o1\b|o3\b|o4\b|ultra|max|pro\b|preview|sonnet-?4|gpt-?5|"
    r"gemini-?2\.5|claude-?4|reasoning|thinking|deepseek.?r1|deep.?research)",
    re.IGNORECASE,
)
_COMPLEX_HINTS = re.compile(
    r"(sonnet|gpt-?4|opus-?3|gemini-?2\.0|claude-?3\.5|qwen-?max|qwen-?plus|"
    r"deepseek.?v3|yi.?large|llama-?3\.1-?70|llama-?3\.1-?405|mistral.?large)",
    re.IGNORECASE,
)
_SIMPLE_HINTS = re.compile(
    r"(haiku|mini|small|nano|lite|flash|8b|7b|3b|1b|mini)",
    re.IGNORECASE,
)
_TRIVIAL_HINTS = re.compile(
    r"(nano|tiny|mini|0\.5b|1b|1\.5b|2b|3b)",
    re.IGNORECASE,
)


def auto_classify_tier(model: ModelInfo) -> str:
    """Smartly assign a model to one of: trivial / simple / complex / expert.

    Decision tree (in order — first match wins):

      1. **Expert signals** — has "reasoning" / "thinking" feature,
         OR huge context (> 200K) + paid, OR name contains opus/o1/o3/...
         → ``expert``
      2. **Trivial signals** — free AND tiny context (< 8K) AND no
         vision/tools/reasoning, OR name is "nano" / "tiny" / "0.5b"...
         → ``trivial``
      3. **Complex signals** — paid AND context >= 32K, OR paid AND
         has vision/tools, OR name is sonnet/gpt-4/opus-3/...
         → ``complex``
      4. **Simple** — everything else (free models with reasonable
         context, paid models with small context, haiku/flash/mini/...)
         → ``simple``
    """
    name = (model.id or "") + " " + (model.name or "")
    _name_lc = name.lower()
    feats = {f.lower() for f in (model.features or [])}
    has_vision = "vision" in feats or any("image" in m for m in model.input_modalities)
    has_tools = "tools" in feats
    has_reasoning = "reasoning" in feats or "thinking" in feats
    ctx = int(model.context_length or 0)

    # ---- 1. expert ----
    if has_reasoning and model.is_free is False:
        return "expert"
    if _EXPERT_HINTS.search(name):
        return "expert"
    if model.is_free is False and ctx >= 500_000:
        return "expert"

    # ---- 2. trivial ----
    # 只把"免费"小模型归入 trivial；付费 mini/nano（如 gpt-4o-mini）
    # 能力并不弱，应进 simple。原代码第三条规则不带 is_free 检查，
    # 会把付费 mini 误判为 trivial。
    if model.is_free and ctx and ctx < 8_000 and not has_vision and not has_tools and not has_reasoning:
        return "trivial"
    if _TRIVIAL_HINTS.search(name) and model.is_free:
        return "trivial"

    # ---- 3. complex ----
    if model.is_free is False and ctx >= 32_000:
        return "complex"
    if model.is_free is False and (has_vision or has_tools):
        return "complex"
    if _COMPLEX_HINTS.search(name):
        return "complex"

    # ---- 4. simple (default) ----
    if _SIMPLE_HINTS.search(name):
        return "simple"
    return "simple"


def rebuild_tiers(
    models: List[ModelInfo],
    provider_prefix: str = "",
    existing: Optional[Dict[str, List[str]]] = None,
    max_per_tier: int = 4,
) -> Dict[str, List[str]]:
    """Take a list of ``ModelInfo`` and rebuild the 4-tier model map.

    Each ModelInfo is classified by ``auto_classify_tier`` and then added
    to that tier's list (up to ``max_per_tier`` entries per tier — keeps
    failover list small and predictable).

    Parameters
    ----------
    models:
        The full set of models from a provider catalog.
    provider_prefix:
        If set, every model id is prefixed with ``"<provider>/"`` so the
        result can be plugged into ``LLMProvider.MODEL_TIERS`` directly.
    existing:
        Optional previous tier map — already-present models are kept in
        place (we only *add* new ones to tiers where there's room).
    """
    out: Dict[str, List[str]] = {
        "trivial": list((existing or {}).get("trivial", [])),
        "simple":  list((existing or {}).get("simple", [])),
        "complex": list((existing or {}).get("complex", [])),
        "expert":  list((existing or {}).get("expert", [])),
    }
    # Sort models so we prefer richer ones within a tier
    def _rank(m: ModelInfo) -> Tuple[int, int, int]:
        # higher tier-priority, more features, more context
        tier_pri = {"trivial": 0, "simple": 1, "complex": 2, "expert": 3}[
            auto_classify_tier(m)
        ]
        return (tier_pri, len(m.features), m.context_length)

    sorted_models = sorted(models, key=_rank, reverse=True)
    for m in sorted_models:
        mid = m.id if not provider_prefix else f"{provider_prefix}/{m.id}"
        tier = m.tier or auto_classify_tier(m)
        if not tier or tier not in out:
            continue
        if mid in out[tier]:
            continue
        # max_per_tier=0 (or negative) means "no cap" — useful when the user
        # explicitly says "rebuild the tiers, I want them all in".
        if max_per_tier <= 0 or len(out[tier]) < max_per_tier:
            out[tier].append(mid)
    return out


def diff_tiers(
    old: Dict[str, List[str]],
    new: Dict[str, List[str]],
) -> Dict[str, Dict[str, List[str]]]:
    """Return per-tier added/removed between two tier maps."""
    diff: Dict[str, Dict[str, List[str]]] = {}
    for tier in ("trivial", "simple", "complex", "expert"):
        old_set = set(old.get(tier, []))
        new_set = set(new.get(tier, []))
        diff[tier] = {
            "added": sorted(new_set - old_set),
            "removed": sorted(old_set - new_set),
        }
    return diff
