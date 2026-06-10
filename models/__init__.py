"""Pluggable LLM provider.

Abstracts OpenRouter, OpenAI, Anthropic, DeepSeek, DashScope, Ollama, and
any OpenAI-compatible endpoint behind one tiny interface.  This directly
enables the router's "pick the cheapest capable model" strategy.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from core.plugin import Plugin

logger = logging.getLogger(__name__)


# Models are organized into four buckets.  The router consults
# ``MODEL_TIERS`` when translating a complexity score into a model choice.
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


class LLMProvider(Plugin):
    """Central LLM caller.

    Users configure their API keys once; this class translates them to HTTP
    calls.  The provider never interprets the prompt — it only talks to the
    backend.  Logic lives in the router plugin.
    """

    name = "llm"

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[httpx.AsyncClient] = None
        self._api_keys: Dict[str, str] = {}
        self._default_model: str = "anthropic/claude-3.5-sonnet-20241022"
        self._default_temperature = 0.3
        self._default_max_tokens = 2048
        self._timeout = 60
        self._retry_count = 3
        self._provider_base_urls = {
            "openrouter": "https://openrouter.ai/api/v1",
            "openai": "https://api.openai.com/v1",
            "anthropic": "https://api.anthropic.com/v1",
            "deepseek": "https://api.deepseek.com/v1",
            "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "ollama": "http://localhost:11434/v1",
        }
        # call stats for self-evolution
        self._call_stats: List[Dict[str, Any]] = []

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

        # allow overriding base URL per provider
        custom_endpoints = llm_cfg.get("base_urls", {}) or {}
        for k, v in custom_endpoints.items():
            self._provider_base_urls[k] = v

        self._client = httpx.AsyncClient(timeout=self._timeout)
        logger.info("LLM provider ready, default model=%s", self._default_model)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
        await super().stop()

    # ---------------------------------------------------------- public API
    def model_for_tier(self, tier: str) -> str:
        """Pick the first available model in a tier, or fall back to default."""
        for model in MODEL_TIERS.get(tier, []):
            provider = model.split("/", 1)[0]
            if self._api_keys.get(provider):
                return model
        logger.warning("no API key for tier=%s, using default model", tier)
        return self._default_model

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Call the LLM, retrying on transient errors.

        Returns
        -------
        dict with keys:
            text     — plain text reply (empty if tool-call)
            tool_calls — optional list of {"name":..., "args":{...}}
            tokens_used — int
            model — the model actually used
        """
        model = model or self._default_model
        temperature = self._default_temperature if temperature is None else temperature
        max_tokens = self._default_max_tokens if max_tokens is None else max_tokens

        provider = model.split("/", 1)[0] if "/" in model else "openai"
        base = self._provider_base_urls.get(provider, self._provider_base_urls["openrouter"])
        api_key = self._api_keys.get(provider) or self._api_keys.get("openrouter")

        last_err: Optional[Exception] = None
        for attempt in range(1, self._retry_count + 1):
            try:
                return await self._do_call(
                    base=base,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    tools=tools,
                    provider=provider,
                )
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning("llm call attempt %d failed: %s", attempt, exc)
                time.sleep(1.2 * attempt)
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
        return {"calls": total, "tokens_used": tokens, "recent": self._call_stats[-30:]}

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
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
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
                "stop_reason": data.get("stop_reason"),
            }
            self._call_stats.append({
                "model": model,
                "tokens_used": tokens_used,
                "t": time.time(),
            })
            return result

        # default path — OpenAI compatible (OpenRouter, OpenAI, DeepSeek,
        # DashScope, Ollama all speak this wire format)
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
        tool_calls = []
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
        self._call_stats.append({
            "model": model,
            "tokens_used": tokens_used,
            "t": time.time(),
        })
        return {
            "text": text.strip(),
            "tool_calls": tool_calls,
            "tokens_used": tokens_used,
            "model": data.get("model", model),
        }
