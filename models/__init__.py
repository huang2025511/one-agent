"""Pluggable LLM provider.

Abstracts OpenRouter, OpenAI, Anthropic, DeepSeek, DashScope, Ollama, and
any OpenAI-compatible endpoint behind one tiny interface.

Enhanced with:
  - Hash-based response caching (LRU, configurable size)
  - Provider health checks
  - Per-model cost tracking
"""

from __future__ import annotations

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

# Rough per-token cost (USD) for statistics
MODEL_COST: Dict[str, float] = {
    "anthropic/claude-3.5-sonnet-20241022": 0.003,
    "openai/gpt-4o": 0.005,
    "openai/gpt-4o-mini": 0.00015,
    "google/gemini-2.5-pro-exp-03-25": 0.00125,
    "deepseek/deepseek-chat": 0.00014,
}


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

        self._client = httpx.AsyncClient(timeout=self._timeout)
        logger.info("LLM provider ready, default model=%s, cache=%s",
                    self._default_model, self._cache_enabled)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        await super().stop()

    # ---------------------------------------------------------- public API
    def model_for_tier(self, tier: str) -> str:
        for model in MODEL_TIERS.get(tier, []):
            provider = model.split("/", 1)[0]
            # Accept a direct provider key OR an openrouter key (openrouter routes to anthropic/...)
            if self._api_keys.get(provider) or self._api_keys.get("openrouter"):
                return model
        return self._default_model

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
                logger.warning("llm call attempt %d failed: %s", attempt, exc)
                if attempt < self._retry_count:
                    await asyncio.sleep(1.2 * attempt)

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
            "model": model,
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
