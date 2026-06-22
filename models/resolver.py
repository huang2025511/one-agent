"""Provider name → base URL resolver.

The user only needs to supply a friendly alias (e.g. ``"sensenova"``) and an
API key.  This module figures out the correct ``https://.../v1`` URL one of
three ways:

  1. **Registry hit** — known providers (OpenAI, Anthropic, DeepSeek,
     SenseNova, Zhipu, Moonshot, Yi, Baichuan, Doubao, Stepfun, etc.)
     return their hard-coded base URL instantly.

  2. **Heuristic probe** — for unknown aliases, we build a few candidate
     hostnames (``sensenova.cn``, ``api.sensenova.cn``, ``sensenova.ai``,
     ...) and try common OpenAI-compatible path layouts (``/v1``,
     ``/compatible-mode/v1``, ``/api/v1``).  The first endpoint that
     answers ``GET /models`` with a 2xx wins.

  3. **Cache** — successful resolutions are memoised per alias in
     ``_PROBE_CACHE`` so we don't probe the same provider twice in a
     process lifetime.

The probe is intentionally cheap (5 s timeout per URL, no retries) — we
never block the user for more than a few seconds even if everything
fails.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


# ============================================================
# Registry: well-known OpenAI-compatible providers.
# Update / extend this table as new providers appear.
# ============================================================
KNOWN_PROVIDERS: Dict[str, str] = {
    # --- US ---
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "cohere": "https://api.cohere.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "together": "https://api.together.xyz/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "perplexity": "https://api.perplexity.ai",
    "xai": "https://api.x.ai/v1",
    "grok": "https://api.x.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "huggingface": "https://api-inference.huggingface.co/models",
    "replicate": "https://api.replicate.com/v1",
    "ai21": "https://api.ai21.com/studio/v1",
    "voyage": "https://api.voyageai.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "vertex": "https://us-central1-aiplatform.googleapis.com/v1",
    # --- China ---
    "deepseek": "https://api.deepseek.com/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "tongyi": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "sensenova": "https://token.sensenova.cn/v1",
    "yi": "https://api.lingyiwanwu.com/v1",
    "lingyiwanwu": "https://api.lingyiwanwu.com/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "chatglm": "https://open.bigmodel.cn/api/paas/v4",
    "moonshot": "https://api.moonshot.cn/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "minimax": "https://api.MiniMax.chat/v1",
    "minimaxi": "https://api.MiniMax.chat/v1",
    "abab": "https://api.MiniMax.chat/v1",
    "baichuan": "https://api.baichuan-ai.com/v1",
    "doubao": "https://ark.cn-beijing.volces.com/api/v3",
    "volcengine": "https://ark.cn-beijing.volces.com/api/v3",
    "ark": "https://ark.cn-beijing.volces.com/api/v3",
    "stepfun": "https://api.stepfun.com/v1",
    "hunyuan": "https://hunyuan.tencent.com/v1",
    "tencent": "https://hunyuan.tencent.com/v1",
    "spark": "https://spark-api-open.xf-yun.com/v1",
    "xfyun": "https://spark-api-open.xf-yun.com/v1",
    "iflytek": "https://spark-api-open.xf-yun.com/v1",
    "wenxin": "https://qianfan.baidubce.com/v2",
    "qianfan": "https://qianfan.baidubce.com/v2",
    "ernie": "https://qianfan.baidubce.com/v2",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "nvapi": "https://integrate.api.nvidia.com/v1",
    # --- local / self-hosted ---
    "ollama": "http://localhost:11434/v1",
    "lmstudio": "http://localhost:1234/v1",
    "vllm": "http://localhost:8000/v1",
    "llamacpp": "http://localhost:8080/v1",
    "text-generation-webui": "http://localhost:5000/v1",
    "localai": "http://localhost:8080/v1",
}


# Path patterns to try (in order) when probing an unknown alias.
_PROBE_PATH_PATTERNS: Tuple[str, ...] = (
    "https://{h}/v1",
    "https://{h}/compatible-mode/v1",
    "https://{h}/api/v1",
    "https://{h}/openapi/v1",
    "https://api.{h}/v1",
    "https://api.{h}/compatible-mode/v1",
    "https://api.{h}/api/v1",
)


@dataclass
class ResolvedProvider:
    """Result of a ``resolve()`` call."""

    provider: str
    base_url: str
    found: bool
    via: str = ""        # "registry" | "probe" | "default" | "cache"


# In-process cache so we never probe the same alias twice.
_PROBE_CACHE: Dict[str, ResolvedProvider] = {}


def _candidate_hosts(name: str) -> List[str]:
    """Build candidate hostnames from an unknown provider alias.

    >>> _candidate_hosts("sensenova")
    ['sensenova.cn', 'sensenova.ai', 'api.sensenova.cn', 'sensenova.com',
     'api.sensenova.ai', 'open.sensenova.cn', 'open.sensenova.com',
     'sensenova', 'api.sensenova.com', 'sensenova.com.cn']
    """
    n = re.sub(r"[^a-z0-9-]+", "", name.lower().strip())
    if not n:
        return []
    return [
        f"{n}.cn",
        f"{n}.ai",
        f"api.{n}.cn",
        f"{n}.com",
        f"api.{n}.ai",
        f"open.{n}.cn",
        f"open.{n}.com",
        f"token.{n}.cn",   # SenseNova / 商汤
        n,
        f"api.{n}.com",
        f"{n}.com.cn",
    ]


# Chinese / friendly name → canonical provider name mapping
# NOTE: This is the SINGLE source of truth for aliases. Do not redeclare
# _PROVIDER_ALIASES elsewhere in this module.
_PROVIDER_ALIASES: Dict[str, str] = {
    # --- China providers (Chinese names) ---
    "英伟达": "nvidia",
    "英伟": "nvidia",
    "商汤": "sensenova",
    "日日新": "sensenova",
    "智谱": "glm",
    "月之暗面": "kimi",
    "零一": "yi",
    "零一万物": "yi",
    "百川": "baichuan",
    "豆包": "doubao",
    "字节": "doubao",
    "腾讯": "hunyuan",
    "混元": "hunyuan",
    "科大讯飞": "spark",
    "讯飞": "spark",
    "百度": "wenxin",
    "文心": "wenxin",
    "阿里": "qwen",
    "通义": "qwen",
    "阶跃": "stepfun",
    "深度求索": "deepseek",
    "谷歌": "google",
    "本地": "ollama",
    # --- English / canonical aliases ---
    "openai": "openai", "gpt": "openai", "chatgpt": "openai", "OpenAI": "openai",
    "anthropic": "anthropic", "claude": "anthropic", "sonnet": "anthropic",
    "haiku": "anthropic", "opus": "anthropic", "Anthropic": "anthropic",
    "google": "google", "gemini": "google", "bard": "google",
    "Gemini": "google", "Google": "google",
    "deepseek": "deepseek", "ds": "deepseek", "DeepSeek": "deepseek",
    "qwen": "qwen", "tongyi": "qwen", "dashscope": "qwen",
    "glm": "glm", "zhipu": "glm", "chatglm": "glm", "ChatGLM": "glm", "GLM": "glm",
    "kimi": "kimi", "moonshot": "kimi", "Moonshot": "kimi", "Kimi": "kimi",
    "yi": "yi", "lingyi": "yi", "lingyiwanwu": "yi",
    "sensenova": "sensenova", "SenseNova": "sensenova",
    "doubao": "doubao", "volcengine": "doubao", "ark": "doubao",
    "hunyuan": "hunyuan", "tencent": "hunyuan",
    "spark": "spark", "xfyun": "spark", "iflytek": "spark",
    "iFLYTEK": "spark",
    "wenxin": "wenxin", "qianfan": "wenxin", "ernie": "wenxin",
    "baichuan": "baichuan",
    "stepfun": "stepfun",
    "minimax": "minimax", "minimaxi": "minimax", "abab": "minimax",
    "nvidia": "nvidia", "nvapi": "nvidia", "NVIDIA": "nvidia",
    "ollama": "ollama", "local": "ollama",
    "openrouter": "openrouter",
    "groq": "groq", "together": "together", "fireworks": "fireworks",
    "mistral": "mistral", "cohere": "cohere", "xai": "xai", "grok": "xai",
    "perplexity": "perplexity", "huggingface": "huggingface",
    "replicate": "replicate",
    "lmstudio": "lmstudio", "vllm": "vllm", "llamacpp": "llamacpp",
    "text-generation-webui": "text-generation-webui", "localai": "localai",
}


def lookup(name: str) -> Optional[str]:
    """Synchronous registry-only lookup.  No network.  Returns URL or None."""
    n = (name or "").lower().strip()
    if not n:
        return None
    # Resolve Chinese / friendly name first
    canonical = _PROVIDER_ALIASES.get(name) or _PROVIDER_ALIASES.get(n)
    if canonical and canonical in KNOWN_PROVIDERS:
        return KNOWN_PROVIDERS[canonical]
    if n in KNOWN_PROVIDERS:
        return KNOWN_PROVIDERS[n]
    # try a few common aliases
    for variant in (n, n.replace(" ", ""), n.replace("-", ""), n.replace("_", "")):
        if variant in KNOWN_PROVIDERS:
            return KNOWN_PROVIDERS[variant]
    return None


def list_known() -> Dict[str, str]:
    """Return a copy of the registry (for ``LLMProvider.list_providers()``)."""
    return dict(KNOWN_PROVIDERS)


async def _probe_one(
    client: httpx.AsyncClient, base_url: str, api_key: str, timeout: float
) -> bool:
    """Return True if ``GET {base_url}/models`` answers with 2xx."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        resp = await client.get(
            f"{base_url.rstrip('/')}/models",
            headers=headers,
            timeout=timeout,
        )
    except Exception as exc:
        logger.debug("provider probe failed for %s: %s", base_url, exc)
        return False
    return 200 <= resp.status_code < 300


async def resolve(
    provider: str,
    api_key: str = "",
    *,
    probe: bool = True,
    timeout: float = 4.0,
    client: Optional[httpx.AsyncClient] = None,
) -> ResolvedProvider:
    """Resolve a provider alias to a working base URL.

    Strategy:
      1. Check the in-process cache.
      2. Check the ``KNOWN_PROVIDERS`` registry (instant).
      3. If ``probe=True`` and step 2 missed, try heuristic hosts.
    """
    name = (provider or "").lower().strip()
    if not name:
        return ResolvedProvider(provider="", base_url="", found=False, via="default")

    # Cache hit — instant return
    if name in _PROBE_CACHE:
        cached = _PROBE_CACHE[name]
        return ResolvedProvider(
            provider=cached.provider,
            base_url=cached.base_url,
            found=cached.found,
            via="cache",
        )

    # Registry hit — no network
    hit = lookup(name)
    if hit:
        result = ResolvedProvider(provider=name, base_url=hit, found=True, via="registry")
        _PROBE_CACHE[name] = result
        return result

    if not probe:
        return ResolvedProvider(provider=name, base_url="", found=False, via="default")

    # Probe candidate URLs
    own_client = client is None
    cli = client or httpx.AsyncClient(timeout=timeout)
    try:
        # Probe in parallel for speed
        candidates: List[str] = []
        for host in _candidate_hosts(name):
            for pattern in _PROBE_PATH_PATTERNS:
                candidates.append(pattern.format(h=host))
        if not candidates:
            return ResolvedProvider(provider=name, base_url="", found=False, via="default")

        results = await asyncio.gather(
            *[_probe_one(cli, url, api_key, timeout) for url in candidates],
            return_exceptions=False,
        )
        for url, ok in zip(candidates, results, strict=False):
            if ok:
                logger.info("resolver: probed %s → %s", name, url)
                result = ResolvedProvider(provider=name, base_url=url, found=True, via="probe")
                _PROBE_CACHE[name] = result
                return result

        result = ResolvedProvider(provider=name, base_url="", found=False, via="default")
        _PROBE_CACHE[name] = result
        return result
    finally:
        if own_client:
            try:
                await cli.aclose()
            except Exception:
                pass


def clear_cache() -> None:
    """Clear the in-process probe cache (mostly for tests)."""
    _PROBE_CACHE.clear()


# ============================================================
# Best-effort provider alias extraction from a user phrase.
# Used by the CLI's `/rebuild_tiers` command so users can type
# Chinese provider names like "为商汤重建分层" and have the
# correct provider be picked.
# ============================================================
# _PROVIDER_ALIASES is defined above (single source of truth).


def _extract_provider_hint(text: str) -> Optional[str]:
    """Best-effort: pull a provider alias out of a user phrase.

    Returns the canonical provider name (matching ``KNOWN_PROVIDERS``) or
    ``None`` if nothing matches.  Used by the CLI to map Chinese phrases
    like "为商汤重建分层" to ``"sensenova"`` without forcing the user to
    type a precise English name.

    Matching is done in two passes:
      1. **Substring** — any alias (Chinese or English) found anywhere in
         the input wins.
      2. **Word** — only if pass 1 missed, try a whole-word match (regex
         with word boundaries) so we don't confuse, e.g., "gpt" inside
         "egpt-3".
    """
    if not text:
        return None
    t = text.strip().lower()
    # Pass 1: substring (Chinese names are 2-3 chars, so substring is safe)
    # Prefer longer aliases first so "minimaxi" beats "minimax".
    for alias in sorted(_PROVIDER_ALIASES.keys(), key=len, reverse=True):
        if alias.lower() in t:
            return _PROVIDER_ALIASES[alias]
    # Pass 2: whole-word match for English aliases
    for alias in sorted(_PROVIDER_ALIASES.keys(), key=len, reverse=True):
        if not any("\u4e00" <= c <= "\u9fff" for c in alias):
            # Build a word-boundary regex; alias is ASCII so \b works.
            import re as _re
            if _re.search(rf"\b{_re.escape(alias.lower())}\b", t):
                return _PROVIDER_ALIASES[alias]
    return None
