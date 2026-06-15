"""Pluggable LLM provider.

Abstracts OpenRouter, OpenAI, Anthropic, DeepSeek, DashScope, Ollama, and
any OpenAI-compatible endpoint behind one tiny interface.

Enhanced with:
  - Hash-based response caching (LRU, configurable size)
  - Provider health checks
  - Per-model cost tracking
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from core.plugin import Plugin
from models.cache import LLMCache
from models.cost_tracker import CostTracker
from models.recommend import RecommendationMixin

logger = logging.getLogger(__name__)


from models.tiers import MODEL_TIERS  # re-export for backward compatibility

# Rough per-token cost (USD per 1K tokens) for statistics
MODEL_COST: Dict[str, float] = {
    # Anthropic
    "anthropic/claude-3.5-sonnet-20241022": 0.003,
    "anthropic/claude-3.5-haiku-20241022":  0.0008,
    "anthropic/claude-haiku-latest":         0.0008,
    "anthropic/claude-4.5-sonnet-20250514":  0.003,
    # OpenAI
    "openai/gpt-4o":                0.005,
    "openai/gpt-4o-mini":           0.00015,
    "openai/gpt-4-turbo":           0.01,
    "openai/o3":                    0.015,
    "openai/o1":                    0.015,
    # Google
    "google/gemini-2.5-pro-exp-03-25":  0.00125,
    "google/gemini-2.0-flash":          0.0001,
    "google/gemini-2.5-pro-preview-05-15": 0.00125,
    # DeepSeek
    "deepseek/deepseek-chat":  0.00014,
    "deepseek/deepseek-reasoner": 0.00055,
    # Qwen / DashScope (Tongyi)
    "qwen/qwen-max":                  0.002,
    "qwen/qwen-plus":                 0.0008,
    "qwen/qwen-2.5-72b-instruct":     0.0004,
    "qwen/qwen-2.5-7b-instruct":      0.0001,
    # SenseNova (商汤)
    "sensenova/deepseek-v4-flash":    0.0001,
    "sensenova/sensenova-6.7-flash-lite": 0.0001,
    # Zhipu GLM (智谱)
    "glm/glm-4":                      0.001,
    "glm/glm-4-plus":                 0.001,
    # Moonshot / Kimi
    "kimi/kimi-k2-0711-preview":      0.0006,
    "kimi/moonshot-v1-128k":          0.001,
    # Yi (零一万物)
    "yi/yi-large":                    0.0008,
    # OpenRouter passthrough
    "openrouter/meta-llama/llama-3-8b-instruct": 0.0002,
}

# HTTP status codes that are safe to retry.
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class LLMProvider(RecommendationMixin, Plugin):
    """Central LLM caller with caching and cost tracking."""

    name = "llm"
    load_priority = 10  # Load early — many plugins depend on it

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[httpx.AsyncClient] = None
        self._api_keys: Dict[str, str] = {}
        self._default_model: str = "anthropic/claude-3.5-sonnet-20241022"
        self._default_temperature = 0.3
        self._default_max_tokens = 2048
        self._timeout = 60
        self._retry_count = 3
        self._cache: Optional[LLMCache] = None
        self._cache_enabled = True
        self._cache_ttl = 3600
        self._call_stats: List[Dict[str, Any]] = []
        self._cost_total: float = 0.0
        self._cost_tracker: Optional[CostTracker] = None
        # Strong refs to background auto-classify tasks so they don't get
        # GC'd before they run (asyncio doesn't keep them alive).
        self._bg_tasks: set = set()
        # Auto-classify cache: last successful reclassify time per provider
        self._auto_classify_timestamps: Dict[str, float] = {}
        # Model name normalization cache: {provider: {lowercase_name: real_id}}
        self._model_name_cache: Dict[str, Dict[str, str]] = {}
        # Endpoint fallback guard: count attempts per provider (max 2)
        self._fallback_count: Dict[str, int] = {}
        # If setup() can't find an event loop, defer the first auto-classify
        # to the next chat_completion() / get_catalog() call.
        self._pending_auto_classify: bool = False
        # Built-in registry of well-known providers.  The resolver
        # module ships a longer list (40+ aliases) that we merge in at
        # runtime so the user only needs to give a friendly name like
        # "sensenova" or "zhipu" — the base URL is auto-filled.
        self._provider_base_urls: Dict[str, str] = {}
        try:
            from .resolver import list_known
            self._provider_base_urls.update(list_known())
        except ImportError:
            # Resolver module missing — fall back to a tiny built-in table
            self._provider_base_urls = {
                "openrouter": "https://openrouter.ai/api/v1",
                "openai": "https://api.openai.com/v1",
                "anthropic": "https://api.anthropic.com/v1",
                "deepseek": "https://api.deepseek.com/v1",
                "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "ollama": "http://localhost:11434/v1",
            }

    # -------------------------------------------------------- lifecycle
    async def setup(self, ctx) -> None:
        await super().setup(ctx)
        llm_cfg = ctx.config.get("llm", {}) or {}
        self._api_keys = llm_cfg.get("api_keys", {}) or {}
        primary = llm_cfg.get("primary_model")
        if primary:
            self._default_model = primary
        self._default_temperature = llm_cfg.get("default_temperature", 0.3)
        self._default_max_tokens = llm_cfg.get("default_max_tokens", 2048)
        self._timeout = llm_cfg.get("timeout", 60)
        self._retry_count = llm_cfg.get("retries", 3)
        # Cache config: read from dedicated llm_cache section first, fall back to llm inline
        cache_cfg = ctx.config.get("llm_cache") or {}
        self._cache_enabled = cache_cfg.get("enabled", llm_cfg.get("cache_enabled", True))
        self._cache_ttl = cache_cfg.get("ttl_seconds", llm_cfg.get("cache_ttl_seconds", 3600))

        if self._cache_enabled:
            max_size = cache_cfg.get("max_size", llm_cfg.get("cache_max_size", 500))
            self._cache = LLMCache(max_size=max_size, ttl_seconds=self._cache_ttl)
            logger.info("LLM cache enabled (size=%d, ttl=%ds)", max_size, self._cache_ttl)

        # Cost tracking
        cost_cfg = (ctx.config.get("llm") or {}).get("cost_tracking") or {}
        if cost_cfg:
            db_path = cost_cfg.get("db_path", "data/memory/costs.db")
            self._cost_tracker = CostTracker(
                db_path=db_path,
                daily_budget=cost_cfg.get("daily_budget", 1.0),
                monthly_budget=cost_cfg.get("monthly_budget", 20.0),
            )
            logger.info("Cost tracking enabled (daily=$%.2f, monthly=$%.2f)",
                        self._cost_tracker._daily_budget,
                        self._cost_tracker._monthly_budget)

        custom_endpoints = llm_cfg.get("base_urls", {}) or {}
        for k, v in custom_endpoints.items():
            self._provider_base_urls[k] = v

        # Connection pool limits — keep the agent from exhausting
        # a provider's keep-alive slots when several plugins call in
        # parallel.  ``max_connections=20`` is enough for typical workloads
        # (router + memory + monitor + rest) without starving any one.
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )
        logger.info("LLM provider ready, default model=%s, cache=%s",
                    self._default_model, self._cache_enabled)
        # ── Auto-classify every provider that has a non-empty key ──────
        # This runs in the background so setup() returns immediately.
        # It populates MODEL_TIERS with newly-discovered models so
        # failover / model_for_tier() work without any user action.
        if llm_cfg.get("auto_classify_on_setup", True):
            import asyncio as _asyncio
            ran = False
            coro = self._auto_classify_all_providers()
            task = self._spawn_bg(coro)
            logger.info("setup auto-classify: spawn_bg returned %s", task)
            if task is not None:
                ran = True
            else:
                try:
                    loop = _asyncio.get_event_loop()
                    if not loop.is_running():
                        loop.run_until_complete(coro)
                        ran = True
                except RuntimeError:
                    # Python 3.12+ — no event loop. Spin one up.
                    try:
                        new_loop = _asyncio.new_event_loop()
                        try:
                            new_loop.run_until_complete(coro)
                            ran = True
                        finally:
                            new_loop.close()
                    except Exception:
                        # Final safety net: avoid "coroutine was never awaited"
                        try:
                            coro.close()
                        except Exception:
                            pass
            if not ran:
                # Defer to the first chat_completion() / get_catalog() call
                self._pending_auto_classify = True

        # ── Self-check: probe primary provider's endpoint ──────────────
        # A quick health check to catch bad URLs early. If the default
        # endpoint returns 403/404, trigger auto-fallback immediately.
        provider = self._infer_primary_provider()
        base = self._provider_base_urls.get(provider)
        api_key = self._api_keys.get(provider)
        if base and api_key and not "${" in (api_key or ""):
            try:
                async with httpx.AsyncClient(timeout=5.0) as probe:
                    r = await probe.get(
                        f"{base.rstrip('/')}/models",
                        headers={"Authorization": f"Bearer {api_key}"},
                    )
                    if r.status_code == 200:
                        logger.info("endpoint check OK: %s → %s", provider, base)
                    else:
                        logger.warning("endpoint check returned %s: %s → %s, trying fallback",
                                       r.status_code, provider, base)
                        await self._try_endpoint_fallback(provider)
            except (httpx.RequestError, httpx.TimeoutException) as exc:
                logger.debug("endpoint check probe failed: %s", exc)

    async def stop(self) -> None:
        # Wait for any in-flight auto-classify background tasks (max 5s)
        if getattr(self, "_bg_tasks", None):
            import asyncio as _asyncio
            pending = list(self._bg_tasks)
            if pending:
                try:
                    await _asyncio.wait_for(
                        _asyncio.gather(*pending, return_exceptions=True),
                        timeout=5.0,
                    )
                except _asyncio.TimeoutError:
                    for t in pending:
                        t.cancel()
        if self._client is not None:
            await self._client.aclose()
        await super().stop()

    # ---------------------------------------------------------- public API
    def _has_usable_key(self, provider: str) -> bool:
        """True if ``provider`` has a non-empty, unexpanded key configured."""
        v = self._api_keys.get(provider) or self._api_keys.get("openrouter")
        if not v or not v.strip() or "${" in v:
            return False
        return True

    def set_api_key(self, provider: str, key: str) -> Dict[str, Any]:
        """Add or update an API key.  Triggers a background reclassify so
        newly-reachable models get slotted into MODEL_TIERS without
        requiring a restart."""
        key = (key or "").strip()
        self._api_keys[provider] = key
        # Make sure we have a base URL we can talk to
        if provider not in self._provider_base_urls:
            # Use the resolver if available, otherwise just leave it —
            # get_catalog() / rebuild_tiers() will return no_api_key in that case
            try:
                from .resolver import resolve
                import asyncio as _asyncio
                try:
                    loop = _asyncio.get_event_loop()
                    if loop.is_running():
                        hint_info = _asyncio.ensure_future(resolve(provider))
                        hint_info.add_done_callback(
                            lambda fut: self._on_provider_resolved(provider, fut.result())
                        )
                except RuntimeError:
                    # No event loop — schedule via new_event_loop for the
                    # background resolve, then register the URL
                    try:
                        new_loop = _asyncio.new_event_loop()
                        try:
                            hint_info = new_loop.run_until_complete(resolve(provider))
                        finally:
                            new_loop.close()
                        self._on_provider_resolved(provider, hint_info)
                    except Exception:
                        pass
            except Exception:
                pass
        # Fire-and-forget background reclassify
        ran = False
        import asyncio as _asyncio
        coro = self._auto_classify_one(provider)
        task = self._spawn_bg(coro)
        if task is not None:
            ran = True
        else:
            try:
                loop = _asyncio.get_event_loop()
                if not loop.is_running():
                    loop.run_until_complete(coro)
                    ran = True
            except RuntimeError:
                # Python 3.12+ raises here when no event loop exists.
                # Spin up a one-shot loop just for the reclassify.
                try:
                    new_loop = _asyncio.new_event_loop()
                    try:
                        new_loop.run_until_complete(coro)
                        ran = True
                    finally:
                        new_loop.close()
                except Exception:
                    try:
                        coro.close()
                    except Exception:
                        pass
        return {"ok": True, "provider": provider, "key_set": bool(key), "reclassified": ran}

    def _on_provider_resolved(self, provider: str, hint_info: Any) -> None:
        """Callback when an async resolve() finishes — register the URL."""
        if hint_info is None:
            return
        try:
            if hint_info.found and hint_info.base_url:
                self._provider_base_urls[provider] = hint_info.base_url
        except Exception:
            pass

    def list_known_providers(self) -> Dict[str, str]:
        """Return the resolver's known provider registry.

        Useful for the CLI ``/providers`` command — shows the user every
        provider we can auto-fill a base URL for.
        """
        try:
            from .resolver import list_known
            return list_known()
        except ImportError:
            return dict(self._provider_base_urls)

    def get_provider_url(self, provider: str) -> Optional[str]:
        """Synchronous lookup of a provider's base URL from the registry."""
        try:
            from .resolver import lookup
            hit = lookup(provider)
            if hit:
                return hit
        except ImportError:
            pass
        return self._provider_base_urls.get(provider)

    # ---------------------------------------------------------- auto-classify
    def _spawn_bg(self, coro) -> Optional[Any]:
        """Schedule a coroutine as a background task with a strong ref so
        asyncio doesn't GC it before it runs.  Returns the Task or None."""
        import asyncio as _asyncio
        # If we're inside a running coroutine, get_running_loop() works;
        # otherwise we have to use get_event_loop() which can return None.
        try:
            loop = _asyncio.get_running_loop()
        except RuntimeError:
            try:
                loop = _asyncio.get_event_loop()
                if not loop.is_running():
                    return None
            except RuntimeError:
                return None
        task = loop.create_task(coro)
        self._bg_tasks.add(task)

        def _done(t):
            self._bg_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.warning("bg auto-classify task failed: %s", exc)

        task.add_done_callback(_done)
        return task

    # ---------------------------------------------------------- catalog access
    def get_catalog(self, provider: Optional[str] = None) -> Any:
        """Return a ModelCatalog for the given provider (or current default).

        Lazy-creates the catalog using the configured base URL and API key
        for that provider.  Returns ``None`` if no API key is available.
        """
        from .catalog import ModelCatalog
        prov = provider or self._infer_primary_provider()
        base = self._provider_base_urls.get(prov)
        if not base:
            return None
        api_key = self._api_keys.get(prov) or self._api_keys.get("openrouter")
        if not api_key or "${" in (api_key or ""):
            return None
        # If we deferred the auto-classify from setup(), do it now
        # (we have an httpx client + event loop available)
        if getattr(self, "_pending_auto_classify", False):
            self._pending_auto_classify = False  # clear first so retries don't loop
            import asyncio as _asyncio
            coro = self._auto_classify_all_providers()
            task = self._spawn_bg(coro)
            if task is None:
                # No running loop — try a one-shot run
                try:
                    loop = _asyncio.get_event_loop()
                    if not loop.is_running():
                        loop.run_until_complete(coro)
                except RuntimeError:
                    try:
                        new_loop = _asyncio.new_event_loop()
                        try:
                            new_loop.run_until_complete(coro)
                        finally:
                            new_loop.close()
                    except Exception:
                        try:
                            coro.close()
                        except Exception:
                            pass
        return ModelCatalog(base_url=base, api_key=api_key, provider=prov)

    def _infer_primary_provider(self) -> str:
        m = self._default_model or ""
        if "/" in m:
            return m.split("/", 1)[0]
        return "openai"

    async def _try_endpoint_fallback(self, provider: str) -> Optional[str]:
        """Probe alternative endpoints when the current one returns 403/404.

        Returns a new working base URL or None if no alternative found.
        Limited to max 2 attempts per provider per session.

        Bypasses the registry lookup (which may return the same broken URL)
        and directly probes candidate hosts from the resolver's heuristic list.
        """
        count = self._fallback_count.get(provider, 0)
        if count >= 2:
            logger.debug("fallback: max attempts reached for %s", provider)
            return None
        self._fallback_count[provider] = count + 1

        api_key = self._api_keys.get(provider)
        if not api_key or "${" in (api_key or ""):
            return None

        logger.info("probing alternative endpoints for provider=%s (attempt %d/2)", provider, count + 1)

        try:
            from .resolver import _candidate_hosts as _hosts, _PROBE_PATH_PATTERNS as _patterns
        except ImportError:
            return None

        # Generate all candidate URLs from the resolver's host+path patterns
        candidates: list[str] = []
        for host in _hosts(provider):
            for pattern in _patterns:
                candidates.append(pattern.format(h=host))

        if not candidates:
            return None

        # Probe in parallel, stop at first success
        import asyncio as _a
        current_base = self._provider_base_urls.get(provider, "")

        async def _probe(url: str) -> Optional[str]:
            try:
                async with httpx.AsyncClient(timeout=3.0) as cli:
                    r = await cli.get(
                        f"{url.rstrip('/')}/models",
                        headers={"Authorization": f"Bearer {api_key}"},
                    )
                    if 200 <= r.status_code < 300:
                        return url
            except (httpx.RequestError, httpx.TimeoutException):
                pass
            return None

        # Probe in batches of 10 to avoid overwhelming network
        for i in range(0, len(candidates), 10):
            batch = candidates[i:i + 10]
            results = await _a.gather(*[_probe(url) for url in batch])
            for url in results:
                if url and url != current_base:
                    logger.info("switched %s endpoint: %s → %s (auto-probe)",
                                 provider, current_base, url)
                    self._provider_base_urls[provider] = url
                    self._model_name_cache.pop(provider, None)
                    # Update the resolver's known list too
                    try:
                        from .resolver import KNOWN_PROVIDERS
                        KNOWN_PROVIDERS[provider] = url
                    except ImportError:
                        pass
                    return url

        logger.warning("fallback probe for %s: no working endpoint found among %d candidates",
                       provider, len(candidates))
        return None

    async def _build_model_name_map(self, provider: str) -> Dict[str, str]:
        """Fetch the real model list from the API and build a case-insensitive
        name → real ID mapping.  Cached per provider to avoid repeated fetches.

        Example: {"deepseek-v4-flash": "deepseek-v4-flash", "deepseek v4 flash": "deepseek-v4-flash"}
        """
        cached = self._model_name_cache.get(provider)
        if cached:
            return cached

        base = self._provider_base_urls.get(provider)
        api_key = self._api_keys.get(provider)
        if not base or not api_key or "${" in (api_key or ""):
            return {}

        mapping: Dict[str, str] = {}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"{base.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if r.status_code == 200:
                    data = r.json()
                    models = data.get("data", data.get("models", []))
                    for m in models:
                        real_id = m.get("id", "")
                        if not real_id:
                            continue
                        # Map: real ID → itself
                        mapping[real_id] = real_id
                        # Map: lowercase → itself
                        mapping[real_id.lower()] = real_id
                        # Map: normalized (strip special chars, lowercase) → itself
                        import re
                        normalized = re.sub(r"[^a-z0-9]", "", real_id.lower())
                        mapping[normalized] = real_id
        except (httpx.RequestError, httpx.TimeoutException, json.JSONDecodeError) as exc:
            logger.debug("model name map for %s failed: %s", provider, exc)

        self._model_name_cache[provider] = mapping
        return mapping

    def _normalize_model_id(self, model: str, provider: str, bare_model: str) -> str:
        """Try to correct a model name that the API doesn't recognize.

        Strategy:
        1. Exact match in mapping → use as-is
        2. Lowercase match → use real ID
        3. Normalized (strip non-alnum) match → use real ID
        4. Fallback: return original bare_model unchanged
        """
        mapping = self._model_name_cache.get(provider, {})
        if not mapping:
            return bare_model

        # Exact match
        if bare_model in mapping:
            return bare_model
        # Case-insensitive
        low = bare_model.lower()
        if low in mapping:
            return mapping[low]
        # Normalized (strip non-alnum, lowercase)
        import re
        norm = re.sub(r"[^a-z0-9]", "", low)
        if norm in mapping:
            return mapping[norm]

        return bare_model

    async def list_models(
        self,
        provider: Optional[str] = None,
        free_only: bool = False,
        paid_only: bool = False,
        min_context: int = 0,
        feature: Optional[str] = None,
        keyword: Optional[str] = None,
        tier: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        cat = self.get_catalog(provider)
        if cat is None:
            return []
        try:
            await cat.refresh()
            items = cat.filter(
                free_only=free_only, paid_only=paid_only,
                min_context=min_context, feature=feature, keyword=keyword,
                tier=tier,
            )
            return [m.to_dict() for m in items]
        finally:
            await cat.aclose()

    # ---------------------------------------------------------- cost tracking helpers
    @staticmethod
    def _parse_model(model: str) -> tuple:
        """Split ``provider/model_id`` into ``(provider, bare_model)``."""
        if "/" in model:
            provider, bare = model.split("/", 1)
            return provider, bare
        return "openai", model

    def _find_cheapest_free_model(self) -> str:
        """Return the cheapest free model available, falling back to the default."""
        # Look through trivial-tier models for one with zero or negligible cost
        candidates = list(MODEL_TIERS.get("trivial", []))
        if not candidates:
            candidates = list(MODEL_TIERS.get("simple", []))
        if not candidates:
            return self._default_model  # nothing available

        # Prefer models that have a configured API key and cost 0 (free)
        for m in candidates:
            prov = m.split("/", 1)[0]
            if self._has_usable_key(prov) and MODEL_COST.get(m, 1.0) == 0.0:
                return m

        # Fall back to the cheapest model with a usable key
        best_model = self._default_model
        best_cost = float("inf")
        for m in candidates:
            prov = m.split("/", 1)[0]
            if self._has_usable_key(prov):
                c = MODEL_COST.get(m, 0.001)
                if c < best_cost:
                    best_cost = c
                    best_model = m

        return best_model

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        use_cache: bool = True,
    ) -> Dict[str, Any]:
        """Call the LLM, with optional caching and automatic retries."""
        model = model or self._default_model
        temperature = self._default_temperature if temperature is None else temperature
        max_tokens = self._default_max_tokens if max_tokens is None else max_tokens

        # Try cache first.
        # tools + temperature are both included in cache key, so different
        # configs don't share entries.  For stateful tools (weather, API calls)
        # callers should pass use_cache=False explicitly.
        if use_cache and self._cache is not None:
            cached = self._cache.get(messages, model, tools, temperature)
            if cached is not None:
                logger.debug("cache hit for model=%s (tools=%s)", model, bool(tools))
                return cached

        # Budget check: if exceeded, auto-downgrade to cheapest free model
        if self._cost_tracker:
            budget = self._cost_tracker.check_budget()
            if budget["overall_exceeded"]:
                logger.warning(
                    "Budget exceeded (daily=$%.4f/%.2f, monthly=$%.4f/%.2f), "
                    "downgrading to free model",
                    budget["daily"]["cost"], budget["daily"]["budget"],
                    budget["monthly"]["cost"], budget["monthly"]["budget"],
                )
                model = self._find_cheapest_free_model()

        provider = model.split("/", 1)[0] if "/" in model else "openai"
        # Use .get() with fallback to avoid KeyError if openrouter is missing
        base = self._provider_base_urls.get(
            provider,
            self._provider_base_urls.get("openrouter", "https://openrouter.ai/api/v1"),
        )
        api_key = self._api_keys.get(provider) or self._api_keys.get("openrouter")
        # Strip the "<provider>/" prefix from the model id — OpenAI-compatible
        # endpoints expect the bare model name (e.g. "deepseek-v4-flash",
        # not "sensenova/deepseek-v4-flash").  Anthropic keeps the prefix
        # stripped in its own branch below.
        bare_model = model.split("/", 1)[1] if "/" in model else model

        # If no API key is available, fail fast instead of retrying 3 times
        if not api_key:
            from i18n import _
            return {
                "text": _("no_api_key", provider=provider),
                "tool_calls": [],
                "tool_calls_raw": [],
                "tokens_used": 0,
                "model": model,
                "failed": True,
            }

        # --- Auto-heal: build model name map and normalize model ID ---
        # Fetches the real model list from the API and builds a
        # case-insensitive mapping so users can type "DeepSeek-V4-Flash"
        # and it maps to the actual "deepseek-v4-flash" automatically.
        try:
            await self._build_model_name_map(provider)
            corrected = self._normalize_model_id(model, provider, bare_model)
            if corrected != bare_model:
                logger.info("auto-corrected model name: %s → %s", bare_model, corrected)
                bare_model = corrected
                # Rebuild the full model string with provider prefix
                model = f"{provider}/{bare_model}"
        except Exception:
            pass  # Best-effort — fall through to original name

        last_err: Optional[Exception] = None
        for attempt in range(1, self._retry_count + 1):
            try:
                result = await self._do_call(
                    base=base, api_key=api_key, model=model,
                    messages=messages, temperature=temperature,
                    max_tokens=max_tokens, tools=tools, provider=provider,
                )
                # Record cost
                cost = MODEL_COST.get(model, 0.001) * (result.get("tokens_used", 0) / 1000)
                self._cost_total += cost
                result["estimated_cost_usd"] = round(cost, 6)
                result["total_cost_usd"] = round(self._cost_total, 6)

                # Persist to cost tracker (best-effort, never blocks the caller)
                if self._cost_tracker:
                    try:
                        provider_name, bare = self._parse_model(model)
                        cost_tracked = self._cost_tracker.record(
                            provider=provider_name,
                            model=bare,
                            tokens_prompt=result.get("tokens_prompt", 0),
                            tokens_completion=result.get("tokens_completion", 0),
                        )
                        result["cost_usd"] = cost_tracked
                    except Exception:
                        logger.debug("cost_tracker record failed", exc_info=True)

                # Store in cache (tools included in key; stateful tools should use use_cache=False)
                if use_cache and self._cache is not None:
                    self._cache.set(messages, model, tools or [], result, temperature)

                return result
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                # Classify: non-retryable errors (4xx auth/bad-request) exit
                # immediately to avoid wasting time on invalid requests.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = (
                    status is None
                    or status in _RETRYABLE_STATUS
                    or isinstance(exc, (asyncio.TimeoutError, ConnectionError))
                )
                if not retryable:
                    # --- Auto-heal: tools not supported (400) ---
                    # Some providers/models don't support function calling.
                    # If we sent tools and got 400, retry without tools.
                    if status == 400 and tools:
                        logger.info("tools not supported by %s, retrying without tools", provider)
                        try:
                            result = await self._do_call(
                                base=base, api_key=api_key, model=model,
                                messages=messages, temperature=temperature,
                                max_tokens=max_tokens, tools=None, provider=provider,
                            )
                            cost = MODEL_COST.get(model, 0.001) * (result.get("tokens_used", 0) / 1000)
                            self._cost_total += cost
                            result["estimated_cost_usd"] = round(cost, 6)
                            result["total_cost_usd"] = round(self._cost_total, 6)
                            if self._cost_tracker:
                                try:
                                    provider_name, bare = self._parse_model(model)
                                    cost_tracked = self._cost_tracker.record(
                                        provider=provider_name, model=bare,
                                        tokens_prompt=result.get("tokens_prompt", 0),
                                        tokens_completion=result.get("tokens_completion", 0),
                                    )
                                    result["cost_usd"] = cost_tracked
                                except Exception:
                                    pass
                            if use_cache and self._cache is not None:
                                self._cache.set(messages, model, [], result, temperature)
                            return result
                        except Exception as retry_exc:
                            retry_status = getattr(getattr(retry_exc, "response", None), "status_code", None)
                            retry_body = ""
                            try:
                                resp_obj = getattr(retry_exc, "response", None)
                                if resp_obj and hasattr(resp_obj, "text"):
                                    retry_body = resp_obj.text[:300]
                            except Exception:
                                pass
                            logger.warning("retry without tools also failed: status=%s body=%s", 
                                          retry_status, retry_body)
                            # Last resort: retry with minimal prompt (no system message)
                            if retry_status == 400:
                                try:
                                    logger.info("last resort: retry with minimal prompt")
                                    # Strip system, tool messages, and tool_calls from assistant
                                    minimal_msgs = []
                                    for m in messages:
                                        role = m.get("role", "")
                                        if role == "tool":
                                            continue
                                        if role == "system":
                                            continue
                                        if role == "assistant" and m.get("tool_calls"):
                                            continue  # skip assistant messages with tool_calls
                                        minimal_msgs.append(m)
                                    if not minimal_msgs:
                                        minimal_msgs = [{"role": "user", "content": "(empty)"}]
                                    result = await self._do_call(
                                        base=base, api_key=api_key, model=model,
                                        messages=minimal_msgs, temperature=temperature,
                                        max_tokens=max_tokens, tools=None, provider=provider,
                                    )
                                    cost = MODEL_COST.get(model, 0.001) * (result.get("tokens_used", 0) / 1000)
                                    self._cost_total += cost
                                    result["estimated_cost_usd"] = round(cost, 6)
                                    result["total_cost_usd"] = round(self._cost_total, 6)
                                    if self._cost_tracker:
                                        try:
                                            provider_name, bare = self._parse_model(model)
                                            cost_tracked = self._cost_tracker.record(
                                                provider=provider_name, model=bare,
                                                tokens_prompt=result.get("tokens_prompt", 0),
                                                tokens_completion=result.get("tokens_completion", 0),
                                            )
                                            result["cost_usd"] = cost_tracked
                                        except Exception:
                                            pass
                                    logger.info("last resort succeeded")
                                    return result
                                except Exception as last_exc:
                                    logger.warning("last resort failed: %s", last_exc)

                    # --- Auto-heal: endpoint fallback ---
                    # When we get 403/404, try probing alternative URLs.
                    # The resolver module has 40+ provider aliases and
                    # candidate host patterns — this is what makes
                    # "give a provider name + key → auto-adapt" work.
                    if status in (403, 404) and self._fallback_count.get(provider, 0) < 2:
                        new_base = await self._try_endpoint_fallback(provider)
                        if new_base:
                            base = new_base
                            # Rebuild model name map with new URL
                            self._model_name_cache.pop(provider, None)
                            try:
                                await self._build_model_name_map(provider)
                                corrected = self._normalize_model_id(model, provider, bare_model)
                                if corrected != bare_model:
                                    bare_model = corrected
                                    model = f"{provider}/{bare_model}"
                            except Exception:
                                pass
                            # Retry once with the new endpoint
                            try:
                                result = await self._do_call(
                                    base=base, api_key=api_key, model=model,
                                    messages=messages, temperature=temperature,
                                    max_tokens=max_tokens, tools=tools, provider=provider,
                                )
                                cost = MODEL_COST.get(model, 0.001) * (result.get("tokens_used", 0) / 1000)
                                self._cost_total += cost
                                result["estimated_cost_usd"] = round(cost, 6)
                                result["total_cost_usd"] = round(self._cost_total, 6)
                                if self._cost_tracker:
                                    try:
                                        provider_name, bare = self._parse_model(model)
                                        cost_tracked = self._cost_tracker.record(
                                            provider=provider_name, model=bare,
                                            tokens_prompt=result.get("tokens_prompt", 0),
                                            tokens_completion=result.get("tokens_completion", 0),
                                        )
                                        result["cost_usd"] = cost_tracked
                                    except Exception:
                                        pass
                                if use_cache and self._cache is not None:
                                    self._cache.set(messages, model, tools or [], result, temperature)
                                return result
                            except Exception:
                                pass  # Fallback also failed, continue to error path
                    logger.warning("llm call non-retryable error (status=%s): %s", status, exc)
                    break
                if attempt < self._retry_count:
                    # Respect Retry-After hint when present, otherwise
                    # exponential backoff with full jitter (1.2^n seconds,
                    # 0..1.2^n uniform random).
                    import random as _rnd
                    retry_after = None
                    if getattr(exc, "response", None) is not None:
                        try:
                            retry_after = float(exc.response.headers.get("Retry-After", "").strip())
                        except (TypeError, ValueError):
                            retry_after = None
                    if retry_after is not None and retry_after > 0:
                        delay = min(retry_after, 30.0)
                    else:
                        delay = _rnd.uniform(0, 1.2 * (2 ** (attempt - 1)))
                    logger.warning(
                        "llm call attempt %d/%d failed (status=%s), retrying in %.2fs: %s",
                        attempt, self._retry_count, status, delay, exc,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.error("llm call gave up after %d attempts: %s", self._retry_count, exc)

        from i18n import _
        return {
            "text": _("service_unavailable"),
            "tool_calls": [],
            "tool_calls_raw": [],
            "tokens_used": 0,
            "model": model,
            "failed": True,
        }

    async def chat_completion_stream(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ):
        """Stream chat completion via SSE. Yields dict chunks:
        {"delta": content, "done": False} for content chunks
        {"delta": "", "done": True, "tokens_used": N} on completion
        {"error": "...", "done": True} on error
        """
        model = model or self._default_model
        temperature = self._default_temperature if temperature is None else temperature
        max_tokens = self._default_max_tokens if max_tokens is None else max_tokens

        provider = model.split("/", 1)[0] if "/" in model else "openai"
        base = self._provider_base_urls.get(
            provider,
            self._provider_base_urls.get("openrouter", "https://openrouter.ai/api/v1"),
        )
        api_key = self._api_keys.get(provider) or self._api_keys.get("openrouter")
        bare_model = model.split("/", 1)[1] if "/" in model else model

        if not api_key:
            yield {"delta": "", "done": True, "error": f"no API key for provider '{provider}'"}
            return

        # Try streaming first; fall back to non-streaming on 400
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        if provider == "anthropic":
            headers = {
                "x-api-key": api_key or "",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            }
            payload: Dict[str, Any] = {
                "model": bare_model,
                "max_tokens": max_tokens,
                "messages": messages,
                "temperature": temperature,
                "stream": True,
            }
            if tools:
                payload["tools"] = tools
            url = f"{base.rstrip('/')}/messages"
            try:
                async with self._client.stream("POST", url, headers=headers, json=payload, timeout=self._timeout) as resp:  # type: ignore[union-attr]
                    if resp.status_code == 400:
                        # Fallback: streaming not supported, use non-streaming
                        logger.info("streaming not supported by anthropic %s, falling back to non-streaming", bare_model)
                        result = await self._do_call(
                            base=base, api_key=api_key, model=model,
                            messages=messages, temperature=temperature,
                            max_tokens=max_tokens, tools=tools, provider=provider,
                        )
                        yield {"delta": result.get("text", ""), "done": False}
                        yield {"delta": "", "done": True, "tokens_used": result.get("tokens_used", 0)}
                        return
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        event_type = data.get("type", "")
                        if event_type == "content_block_delta":
                            delta = data.get("delta", {})
                            text_delta = delta.get("text", "")
                            if text_delta:
                                yield {"delta": text_delta, "done": False}
                        elif event_type == "message_delta":
                            usage = (data.get("usage") or {})
                            tokens_used = usage.get("output_tokens", 0)
                            yield {"delta": "", "done": True, "tokens_used": tokens_used}
                        elif event_type == "message_stop":
                            yield {"delta": "", "done": True, "tokens_used": 0}
                    # Ensure we always yield a final done event
                    yield {"delta": "", "done": True, "tokens_used": 0}
            except httpx.HTTPStatusError as exc:
                yield {"delta": "", "done": True, "error": f"stream error: {exc.response.status_code}"}
                return
            except (httpx.RequestError, httpx.TimeoutException, Exception) as exc:
                logger.warning("stream error for anthropic %s: %s", bare_model, exc)
                yield {"delta": "", "done": True, "error": str(exc)}
                return
        else:
            # Default — OpenAI-compatible
            payload = {
                "model": bare_model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            }
            if tools:
                payload["tools"] = tools
            url = f"{base.rstrip('/')}/chat/completions"
            try:
                async with self._client.stream("POST", url, headers=headers, json=payload, timeout=self._timeout) as resp:  # type: ignore[union-attr]
                    if resp.status_code == 400:
                        # Fallback: streaming not supported, use non-streaming
                        logger.info("streaming not supported by %s/%s, falling back to non-streaming", provider, bare_model)
                        result = await self._do_call(
                            base=base, api_key=api_key, model=model,
                            messages=messages, temperature=temperature,
                            max_tokens=max_tokens, tools=tools, provider=provider,
                        )
                        yield {"delta": result.get("text", ""), "done": False}
                        yield {"delta": "", "done": True, "tokens_used": result.get("tokens_used", 0)}
                        return
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        choices = data.get("choices") or []
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "") or ""
                            if content:
                                yield {"delta": content, "done": False}
                        usage = data.get("usage")
                        if usage:
                            tokens_used = usage.get("total_tokens", 0)
                            yield {"delta": "", "done": True, "tokens_used": tokens_used}
                            return
                    # Ensure we always yield a final done event
                    yield {"delta": "", "done": True, "tokens_used": 0}
            except httpx.HTTPStatusError as exc:
                yield {"delta": "", "done": True, "error": f"stream error: {exc.response.status_code}"}
                return
            except (httpx.RequestError, httpx.TimeoutException, Exception) as exc:
                logger.warning("stream error for %s/%s: %s", provider, bare_model, exc)
                yield {"delta": "", "done": True, "error": str(exc)}
                return

    def stats(self) -> Dict[str, Any]:
        total = len(self._call_stats)
        tokens = sum(c["tokens_used"] for c in self._call_stats)
        return {
            "calls": total,
            "tokens_used": tokens,
            "total_cost_usd": round(self._cost_total, 6),
            "cache": self._cache.stats() if self._cache else {},
            "recent": self._call_stats[-30:],
        }

    def clear_cache(self) -> Dict[str, Any]:
        if self._cache:
            self._cache.clear()
        return {"cleared": True}

    # ----------------------------------------------------------- internal
    @staticmethod
    def _normalize_vision_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Transform messages with ``image_base64`` in content into the
        OpenAI vision API format.

        When a message's ``content`` is a dict containing ``image_base64``,
        replace it with::

            content = [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": "data:{mime};base64,{b64}"}},
            ]

        Messages that don't need transformation are returned unchanged.
        """
        result: List[Dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, dict) and "image_base64" in content:
                b64 = content.get("image_base64", "")
                mime = content.get("mime_type", "image/png")
                question = content.get("prompt", content.get("question", "请描述这张图片"))
                new_msg = dict(msg)
                new_msg["content"] = [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]
                result.append(new_msg)
            else:
                result.append(msg)
        return result

    async def _do_call(
        self,
        *,
        base: str,
        api_key: Optional[str],
        model: str,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: int,
        tools: Optional[List[Dict[str, Any]]],
        provider: str,
    ) -> Dict[str, Any]:
        # Normalize vision messages: convert {image_base64, mime_type, prompt}
        # dicts into the OpenAI vision API format.
        messages = self._normalize_vision_messages(messages)

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if provider == "anthropic":
            headers = {
                "x-api-key": api_key or "",
                "Content-Type": "application/json",
                "anthropic-version": "2023-06-01",
            }
            payload = {
                "model": model.split("/", 1)[1] if "/" in model else model,
                "max_tokens": max_tokens,
                "messages": messages,
                "temperature": temperature,
            }
            if tools:
                payload["tools"] = tools
            resp = await self._client.post(  # type: ignore[union-attr]
                f"{base.rstrip('/')}/messages", headers=headers, json=payload
            )
            resp.raise_for_status()
            data = resp.json()
            text = ""
            tool_calls: List[Dict[str, Any]] = []
            for block in data.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "name": block.get("name"),
                        "args": block.get("input", {}),
                        "id": block.get("id"),
                    })
            tokens_used = (data.get("usage") or {}).get("total_tokens", 0)
            usage = data.get("usage") or {}
            tokens_prompt = usage.get("input_tokens", 0)
            tokens_completion = usage.get("output_tokens", 0)
            result = {
                "text": text.strip(),
                "tool_calls": tool_calls,
                "tool_calls_raw": tool_calls,  # Anthropic: same format
                "tokens_used": tokens_used,
                "tokens_prompt": tokens_prompt,
                "tokens_completion": tokens_completion,
                "model": data.get("model", model),
            }
            self._call_stats.append({"model": model, "tokens_used": tokens_used, "t": time.time()})
            # Cap stats to prevent unbounded memory growth
            if len(self._call_stats) > 1000:
                self._call_stats = self._call_stats[-500:]
            return result

        # Default — OpenAI compatible
        # Strip the "<provider>/" prefix from the model id — OpenAI-compatible
        # endpoints expect the bare model name (e.g. "deepseek-v4-flash",
        # not "sensenova/deepseek-v4-flash").
        bare_model = model.split("/", 1)[1] if "/" in model else model
        payload = {
            "model": bare_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        resp = await self._client.post(  # type: ignore[union-attr]
            f"{base.rstrip('/')}/chat/completions", headers=headers, json=payload
        )
        resp.raise_for_status()
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        text = msg.get("content", "") or ""
        tool_calls: List[Dict[str, Any]] = []
        tool_calls_raw: List[Dict[str, Any]] = []  # Original API format for message history
        for tc in msg.get("tool_calls") or []:
            tool_calls_raw.append(tc)  # Preserve original format
            try:
                args = json.loads(tc.get("function", {}).get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}
            tool_calls.append({
                "name": tc.get("function", {}).get("name"),
                "args": args,
                "id": tc.get("id"),
            })
        tokens_used = (data.get("usage") or {}).get("total_tokens", 0)
        usage = data.get("usage") or {}
        tokens_prompt = usage.get("prompt_tokens", 0)
        tokens_completion = usage.get("completion_tokens", 0)
        self._call_stats.append({"model": model, "tokens_used": tokens_used, "t": time.time()})
        # Cap stats to prevent unbounded memory growth
        if len(self._call_stats) > 1000:
            self._call_stats = self._call_stats[-500:]
        return {
            "text": text.strip(),
            "tool_calls": tool_calls,
            "tool_calls_raw": tool_calls_raw,
            "tokens_used": tokens_used,
            "tokens_prompt": tokens_prompt,
            "tokens_completion": tokens_completion,
            "model": data.get("model", model),
        }
