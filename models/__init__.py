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
from collections import OrderedDict
from typing import Any, Dict, List, Optional

import httpx

from core.plugin import Plugin

logger = logging.getLogger(__name__)


MODEL_TIERS: Dict[str, List[str]] = {
    "trivial": [
        "openrouter/meta-llama/llama-3-8b-instruct",
        "anthropic/claude-haiku-latest",
        "deepseek/deepseek-chat",
        "qwen/qwen-2.5-7b-instruct",
    ],
    "simple": [
        "anthropic/claude-3.5-haiku-20241022",
        "openai/gpt-4o-mini",
        "google/gemini-2.0-flash",
    ],
    "complex": [
        "anthropic/claude-3.5-sonnet-20241022",
        "openai/gpt-4o",
        "google/gemini-2.5-pro-exp-03-25",
    ],
    "expert": [
        "anthropic/claude-4.5-sonnet-20250514",
        "openai/o3",
        "google/gemini-2.5-pro-preview-05-15",
    ],
}

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
    "sensenova/DeepSeek-V4-Flash":    0.0001,
    "sensenova/SenseNova-6.7-Flash-Lite": 0.0001,
    "sensenova/SenseNova-U1-Fast":    0.0001,
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


class _CacheEntry:
    """Simple LRU cache entry with TTL support."""

    def __init__(self, value: Dict[str, Any], ttl: float = 3600) -> None:
        self.value = value
        self.created_at = time.time()
        self.ttl = ttl
        self.hits = 0

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl


class LLMCache:
    """LRU cache with TTL for LLM responses."""

    def __init__(self, max_size: int = 500, ttl_seconds: float = 3600) -> None:
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._store: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _make_key(messages, model, tools, temperature=None) -> str:
        import hashlib
        payload = json.dumps({
            "messages": messages,
            "model": model,
            "tools": tools,
            "temperature": temperature,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:32]

    def get(self, messages, model, tools=None, temperature=None) -> Optional[Dict[str, Any]]:
        key = self._make_key(messages, model, tools, temperature)
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        if entry.is_expired():
            del self._store[key]
            self._misses += 1
            return None
        self._hits += 1
        entry.hits += 1
        # Move to end (most recently used)
        self._store.move_to_end(key)
        return entry.value

    def set(self, messages, model, tools, value: Dict[str, Any], temperature=None) -> None:
        key = self._make_key(messages, model, tools, temperature)
        entry = _CacheEntry(value, self._ttl)
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = entry
        if len(self._store) > self._max_size:
            # Evict oldest
            self._store.popitem(last=False)

    def stats(self) -> Dict[str, Any]:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total > 0 else 0.0,
            "size": len(self._store),
            "max_size": self._max_size,
        }

    def clear(self) -> None:
        self._store.clear()
        self._hits = 0
        self._misses = 0


class LLMProvider(Plugin):
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
        # Strong refs to background auto-classify tasks so they don't get
        # GC'd before they run (asyncio doesn't keep them alive).
        self._bg_tasks: set = set()
        # Auto-classify cache: last successful reclassify time per provider
        self._auto_classify_timestamps: Dict[str, float] = {}
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
        except Exception:  # noqa: BLE001
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

    def model_for_tier(self, tier: str) -> str:
        for model in MODEL_TIERS.get(tier, []):
            provider = model.split("/", 1)[0]
            # Accept a direct provider key OR an openrouter key (openrouter routes to anthropic/...)
            if self._api_keys.get(provider) or self._api_keys.get("openrouter"):
                return model
        return self._default_model

    async def rebuild_tiers(
        self,
        provider: Optional[str] = None,
        max_per_tier: int = 4,
        persist: bool = True,
    ) -> Dict[str, Any]:
        """Auto-classify every model on the given provider into 4 tiers.

        Pulls the live model list from the provider's ``/v1/models`` endpoint,
        runs ``auto_classify_tier()`` on each entry, and rewrites
        ``MODEL_TIERS`` so that adding a new model automatically slots it
        into the right tier (free / small → ``trivial``; paid big →
        ``complex`` / ``expert``; etc.).

        Returns a dict with the new tier map + a per-tier diff vs the old
        one so the CLI can show "I added X to expert, removed Y from complex".
        """
        from .catalog import ModelCatalog, rebuild_tiers as _rebuild, diff_tiers as _diff
        prov = provider or self._infer_primary_provider()
        cat = self.get_catalog(prov)
        if cat is None:
            return {
                "ok": False,
                "error": f"no API key configured for provider '{prov}'",
                "provider": prov,
            }
        try:
            n = await cat.refresh(force=True)
            if n == 0:
                return {
                    "ok": False,
                    "error": f"could not fetch model list from {prov}",
                    "provider": prov,
                }
            old = {k: list(v) for k, v in MODEL_TIERS.items()}
            new = _rebuild(
                cat.all(),
                provider_prefix=prov,
                existing=old,
                max_per_tier=max_per_tier,
            )
            # Mutate the module-level MODEL_TIERS so model_for_tier() picks it up
            for k, v in new.items():
                MODEL_TIERS[k] = list(v)
            if persist:
                try:
                    cfg = getattr(self, "_config", None) or {}
                    if isinstance(cfg, dict):
                        cfg.setdefault("llm", {})["model_tiers"] = {
                            k: list(v) for k, v in new.items()
                        }
                        self._config = cfg
                except Exception as exc:  # noqa: BLE001
                    logger.debug("rebuild_tiers persist failed: %s", exc)
            return {
                "ok": True,
                "provider": prov,
                "model_count": n,
                "tiers": new,
                "diff": _diff(old, new),
            }
        finally:
            await cat.aclose()

    async def recommend_for(
        self, provider: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return per-capability best-model picks for ``provider``.

        The catalog is force-refreshed first, so newly-added models
        show up immediately.  Output looks like::

            {
              "ok": True,
              "provider": "sensenova",
              "model_count": 12,
              "recommendations": {
                  "best_paid":          {"id": "...", "tier": "complex", "caps": [...]},
                  "best_free":          {"id": "...", "tier": "trivial", "caps": [...]},
                  "best_for_text":      {"id": "..."},
                  "best_for_vision":    {"id": "..."},
                  "best_for_image":     None,
                  ...
              }
            }

        Categories with no qualifying model get ``None`` so the caller
        can show "no vision model on this provider" without crashing.
        """
        from .catalog import ModelCatalog
        from .capabilities import (
            RECOMMEND_CATEGORIES, describe_capabilities,
        )
        prov = provider or self._infer_primary_provider()
        cat = self.get_catalog(prov)
        if cat is None:
            return {
                "ok": False,
                "error": f"no API key / base URL configured for provider '{prov}'",
                "provider": prov,
                "recommendations": {},
            }
        try:
            n = await cat.refresh(force=True)
            if n == 0:
                return {
                    "ok": False,
                    "error": f"could not fetch model list from {prov}",
                    "provider": prov,
                    "recommendations": {},
                }
            recs = cat.recommend()
            # Convert to JSON-friendly form
            out: Dict[str, Any] = {}
            for cat_name, m in recs.items():
                if m is None:
                    out[cat_name] = None
                    continue
                out[cat_name] = {
                    "id": m.id,
                    "name": m.name,
                    "tier": m.tier,
                    "is_free": m.is_free,
                    "context_length": m.context_length,
                    "capabilities": describe_capabilities(m.capabilities),
                    "capabilities_list": sorted(m.capabilities),
                }
            return {
                "ok": True,
                "provider": prov,
                "model_count": n,
                "categories": {
                    k: v.get("label", k) if isinstance(v, dict) else v
                    for k, v in RECOMMEND_CATEGORIES.items()
                },
                "recommendations": out,
            }
        finally:
            await cat.aclose()

    def list_known_providers(self) -> Dict[str, str]:
        """Return the resolver's known provider registry.

        Useful for the CLI ``/providers`` command — shows the user every
        provider we can auto-fill a base URL for.
        """
        try:
            from .resolver import list_known
            return list_known()
        except Exception:  # noqa: BLE001
            return dict(self._provider_base_urls)

    def get_provider_url(self, provider: str) -> Optional[str]:
        """Synchronous lookup of a provider's base URL from the registry."""
        try:
            from .resolver import lookup
            hit = lookup(provider)
            if hit:
                return hit
        except Exception:  # noqa: BLE001
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

    async def _auto_classify_all_providers(self, max_per_tier: int = 0) -> Dict[str, Any]:
        """One-shot auto-classify across every provider with a usable key.

        Iterates over self._provider_base_urls and reclassifies each one
        that has a non-empty key.  Failures are logged but never raised —
        one bad provider must not stop the rest.

        Returns a per-provider summary so callers (e.g. the CLI) can show
        the user "I auto-classified 3 providers: openrouter, sensenova, ...".
        """
        logger.debug("auto_classify_all_providers: starting")
        results: Dict[str, Any] = {}
        # Process providers in a stable order
        for prov in sorted(self._provider_base_urls.keys()):
            if not self._has_usable_key(prov):
                logger.debug("auto_classify_all: skip %s (no key)", prov)
                continue
            try:
                logger.debug("auto_classify_all: classifying %s", prov)
                r = await self._auto_classify_one(prov, max_per_tier=max_per_tier)
                results[prov] = r
                logger.debug("auto_classify_all: %s → %s", prov, r.get("ok"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto_classify %s failed: %s", prov, exc)
                results[prov] = {"ok": False, "error": str(exc)}
        # Clear pending flag
        if getattr(self, "_pending_auto_classify", False):
            self._pending_auto_classify = False
        logger.debug("auto_classify_all_providers: done, results=%s", list(results.keys()))
        return results

    async def _auto_classify_one(
        self, provider: str, max_per_tier: int = 0,
    ) -> Dict[str, Any]:
        """Auto-classify a single provider, silently skipping on failure.

        This is the workhorse used by:
          * ``setup()`` at startup
          * ``set_api_key()`` when a new key is added
          * The first call to ``get_catalog()`` / ``chat_completion()``
            if setup() couldn't find an event loop
        """
        if not self._has_usable_key(provider):
            return {"ok": False, "provider": provider, "skipped": "no_key"}
        # Don't reclassify the same provider within the TTL window
        last = self._auto_classify_timestamps.get(provider, 0.0)
        import time as _t
        now = _t.time()
        if now - last < 30:
            return {"ok": True, "provider": provider, "cached": True}
        try:
            r = await self.rebuild_tiers(provider=provider, max_per_tier=max_per_tier)
            # Only record the timestamp on success — a failure should
            # be retryable immediately (e.g. transient network blip).
            if r.get("ok"):
                self._auto_classify_timestamps[provider] = now
                n = r.get("model_count", 0)
                tier_counts = {
                    t: len(r["tiers"].get(t, [])) for t in ("trivial", "simple", "complex", "expert")
                }
                logger.info(
                    "auto-classify %s: %d models → trivial=%d simple=%d complex=%d expert=%d",
                    provider, n, tier_counts["trivial"], tier_counts["simple"],
                    tier_counts["complex"], tier_counts["expert"],
                )
            return r
        except Exception as exc:  # noqa: BLE001
            logger.debug("auto_classify_one %s: %s", provider, exc)
            return {"ok": False, "provider": provider, "error": str(exc)}

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

        provider = model.split("/", 1)[0] if "/" in model else "openai"
        base = self._provider_base_urls.get(provider, self._provider_base_urls["openrouter"])
        api_key = self._api_keys.get(provider) or self._api_keys.get("openrouter")
        # Strip the "<provider>/" prefix from the model id — OpenAI-compatible
        # endpoints expect the bare model name (e.g. "deepseek-v4-flash",
        # not "sensenova/deepseek-v4-flash").  Anthropic keeps the prefix
        # stripped in its own branch below.
        bare_model = model.split("/", 1)[1] if "/" in model else model

        # If no API key is available, fail fast instead of retrying 3 times
        if not api_key:
            return {
                "text": f"[no API key configured for provider '{provider}']",
                "tool_calls": [],
                "tokens_used": 0,
                "model": model,
                "failed": True,
            }

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

        return {
            "text": f"[upstream unreachable: {last_err}]",
            "tool_calls": [],
            "tokens_used": 0,
            "model": model,
            "failed": True,
        }

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
            result = {
                "text": text.strip(),
                "tool_calls": tool_calls,
                "tokens_used": tokens_used,
                "model": data.get("model", model),
            }
            self._call_stats.append({"model": model, "tokens_used": tokens_used, "t": time.time()})
            # Cap stats to prevent unbounded memory growth
            if len(self._call_stats) > 1000:
                self._call_stats = self._call_stats[-500:]
            return result

        # Default — OpenAI compatible
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
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc.get("function", {}).get("arguments", "{}"))
            except Exception:
                args = {}
            tool_calls.append({
                "name": tc.get("function", {}).get("name"),
                "args": args,
                "id": tc.get("id"),
            })
        tokens_used = (data.get("usage") or {}).get("total_tokens", 0)
        self._call_stats.append({"model": model, "tokens_used": tokens_used, "t": time.time()})
        # Cap stats to prevent unbounded memory growth
        if len(self._call_stats) > 1000:
            self._call_stats = self._call_stats[-500:]
        return {
            "text": text.strip(),
            "tool_calls": tool_calls,
            "tokens_used": tokens_used,
            "model": data.get("model", model),
        }
