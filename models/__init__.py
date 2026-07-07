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
import os
import re
import sqlite3
import time
from collections import deque
from typing import Any, Dict, List, Optional, Set

import httpx

from core.plugin import Plugin
from models.cache import LLMCache
from models.cost_tracker import CostTracker
from models.tiers import MODEL_TIERS, MODEL_COST
from models.recommend import RecommendationMixin


logger = logging.getLogger(__name__)

# Default timeout values in seconds
DEFAULT_TIMEOUT = 60
DEFAULT_CACHE_TTL = 3600
DEFAULT_RETRY_COUNT = 3

# Circuit breaker configuration
CIRCUIT_BREAKER_FAILURE_THRESHOLD = 5
CIRCUIT_BREAKER_RECOVERY_TIMEOUT = 60.0

# Connection pool limits
MAX_CONNECTIONS = 20
MAX_KEEPALIVE_CONNECTIONS = 10

# Stats limits
MAX_CALL_STATS_SIZE = 1000
CALL_STATS_TRIM_SIZE = 500

__all__ = [
    "LLMProvider",
    "MODEL_TIERS",
    "MODEL_COST",
]

# HTTP status codes that are safe to retry.
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class CircuitBreaker:
    """断路器模式：防止对故障服务的重复调用。

    状态转换：
    - CLOSED（正常）：连续失败达到阈值时转为 OPEN
    - OPEN（熔断）：快速失败，不尝试调用。超时后转为 HALF_OPEN
    - HALF_OPEN（半开）：尝试一次调用，成功则转为 CLOSED，失败则回到 OPEN
    """

    def __init__(self, failure_threshold: int = CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                 recovery_timeout: float = CIRCUIT_BREAKER_RECOVERY_TIMEOUT):
        assert failure_threshold > 0, "failure_threshold must be positive"
        assert recovery_timeout >= 0, "recovery_timeout must be non-negative"
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN

    def can_execute(self) -> bool:
        """检查是否允许执行调用。"""
        if self.state == "CLOSED":
            return True
        elif self.state == "OPEN":
            # 检查是否超过恢复时间
            if time.time() - self.last_failure_time >= self.recovery_timeout:
                self.state = "HALF_OPEN"
                return True
            return False
        else:  # HALF_OPEN
            return True

    def record_success(self):
        """记录成功调用。"""
        self.failure_count = 0
        self.state = "CLOSED"

    def record_failure(self):
        """记录失败调用。"""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.state == "HALF_OPEN":
            # 半开状态失败，回到 OPEN
            self.state = "OPEN"
        elif self.failure_count >= self.failure_threshold:
            # 连续失败达到阈值，转为 OPEN
            self.state = "OPEN"
            logger.error(
                "Circuit breaker OPEN after %d failures (will recover in %.0fs)",
                self.failure_count, self.recovery_timeout
            )


class LLMProvider(RecommendationMixin, Plugin):
    """Central LLM caller with caching and cost tracking."""

    name = "llm"
    load_priority = 10  # Load early — many plugins depend on it

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[httpx.AsyncClient] = None
        self._api_keys: Dict[str, str] = {}
        # 修复：默认 model 从 anthropic 改为 sensenova（商汤）。
        # 与 LLMConfig / default_config.yaml / setup_wizard 默认一致。
        # 之前默认 anthropic 但多数用户不配 anthropic key，导致
        # model_for_tier 回退到无 key 的 anthropic → 0 tokens。
        self._default_model: str = "sensenova/sensenova-6.7-flash-lite"
        self._default_temperature = 0.3
        self._default_max_tokens = 2048
        self._timeout = DEFAULT_TIMEOUT
        self._retry_count = DEFAULT_RETRY_COUNT
        self._cache: Optional[LLMCache] = None
        self._cache_enabled = True
        self._cache_ttl = DEFAULT_CACHE_TTL
        self._call_stats: deque[Dict[str, Any]] = deque(maxlen=MAX_CALL_STATS_SIZE)
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
        # Circuit breakers per provider: {provider: CircuitBreaker}
        self._circuit_breakers: Dict[str, CircuitBreaker] = {}
        # 已被 401 拒绝的 provider 集合（key 失效/无权限）。
        # 避免每次请求都拿坏 key 去试一遍 → 浪费往返 + 拖慢响应。
        # set_api_key() 更新 key 后会清除对应 provider。
        self._invalid_keys: Set[str] = set()
        # 已验证存在的模型集合（由 auto_classify 后台任务填充）。
        # 用途：model_for_tier() 选中模型后，用它验证模型是否真实存在，
        # 避免选中硬编码在 default_config.yaml 但 provider 已下线/改名的模型
        # （如 sensenova/tiny 是虚构模型名，调用必返回 404）。
        # 结构：{provider: {model_id_lower, ...}}。auto_classify 还没跑时为空，
        # 此时 model_for_tier 不做验证（向后兼容）。
        self._verified_models: Dict[str, Set[str]] = {}
        # Models known to NOT support tool calling (cached after first 400)
        # so we don't waste a failed request every time.  Key is bare model name.
        self._no_tools_models: Set[str] = set()
        # Models known to SUPPORT tool calling (exceptions to pattern-based rules).
        # Some models like deepseek-v4-flash contain "-flash" but do support tools.
        self._tools_models: Set[str] = {
            "deepseek-v4-flash",
        }
        # Heuristic patterns for models that likely don't support tools.
        # These are checked in addition to the explicit _no_tools_models set.
        self._no_tools_patterns = [
            re.compile(r"-flash", re.IGNORECASE),
            re.compile(r"-lite", re.IGNORECASE),
            re.compile(r"flash[-_]?lite", re.IGNORECASE),
            re.compile(r"-mini", re.IGNORECASE),
            re.compile(r"-tiny", re.IGNORECASE),
            re.compile(r"-small", re.IGNORECASE),
        ]
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
        # 保存 config 引用，供 rebuild_tiers(persist=True) 持久化使用。
        # 之前 LLMProvider 从未设置 self._config，导致 recommend.py 的
        # getattr(self, "_config", None) 永远返回 None，persist 完全失效。
        self._config = ctx.config
        llm_cfg = ctx.config.get("llm", {}) or {}
        self._api_keys = llm_cfg.get("api_keys", {}) or {}
        primary = llm_cfg.get("primary_model")
        if primary:
            self._default_model = primary
        # 读取 config 显式声明的 primary_provider，供 _infer_primary_provider 使用。
        # 修复 bug：之前只从 model 名字推断 provider，对 nvidia 等
        # "vendor/model" 格式的 model id 会误判 provider。
        self._primary_provider = (llm_cfg.get("primary_provider") or "").strip() or None
        # 修复：加载 config 的 model_tiers 到运行时 MODEL_TIERS。
        # 之前 config 里的 model_tiers 是"装饰性"的——只有 rebuild_tiers
        # （从 provider /models 端点拉取后）才会写 MODEL_TIERS，启动时根本
        # 不读 config 的 model_tiers。导致用户在 default_config.yaml 把
        # sensenova/tiny 加进 trivial 也无效（运行时用的是 tiers.py 硬编码
        # 的旧列表）。现在 setup 时把 config 的 model_tiers merge 进来，
        # 用户改 config 即时生效，不依赖 rebuild_tiers。
        cfg_tiers = llm_cfg.get("model_tiers") or {}
        if isinstance(cfg_tiers, dict):
            for tier_name, models in cfg_tiers.items():
                if isinstance(models, list):
                    MODEL_TIERS[tier_name] = list(models)
            logger.info("loaded model_tiers from config: %s",
                        {k: len(v) for k, v in MODEL_TIERS.items()})
        self._default_temperature = llm_cfg.get("default_temperature", 0.3)
        self._default_max_tokens = llm_cfg.get("default_max_tokens", 2048)
        self._timeout = llm_cfg.get("timeout", 60)
        self._retry_count = llm_cfg.get("retries", 3)

        # Fallback chain configuration
        raw_fallback = llm_cfg.get("fallback_chain", []) or []
        self._fallback_chain: List[Dict[str, str]] = []
        for fb in raw_fallback:
            if isinstance(fb, str):
                self._fallback_chain.append({"model": fb})
            elif isinstance(fb, dict) and fb.get("model"):
                self._fallback_chain.append(dict(fb))
        if self._fallback_chain:
            logger.info("LLM fallback chain configured: %s", self._fallback_chain)

        # Pre-seed models known to NOT support tool calling (based on model name).
        # This avoids wasting a failed request on the first turn.
        # Note: deepseek-v4-flash IS in the whitelist (_tools_models) because it supports tools.
        _KNOWN_NO_TOOLS = [
            "sensenova-6.7-flash-lite",
            "sensenova-6.7-flash", "sensenova-default",
        ]
        for m in _KNOWN_NO_TOOLS:
            self._no_tools_models.add(m)

        # Cache config: read from dedicated llm_cache section first, fall back to llm inline
        cache_cfg = ctx.config.get("llm_cache") or {}
        self._cache_enabled = cache_cfg.get("enabled", llm_cfg.get("cache_enabled", True))
        self._cache_ttl = cache_cfg.get("ttl_seconds", llm_cfg.get("cache_ttl_seconds", 3600))

        if self._cache_enabled:
            max_size = cache_cfg.get("max_size", llm_cfg.get("cache_max_size", 500))
            self._cache = LLMCache(max_size=max_size, ttl_seconds=self._cache_ttl)
            logger.info("LLM cache enabled (size=%d, ttl=%ds)", max_size, self._cache_ttl)

        # Cost tracking
        # 修复 bug：之前 db_path 默认 "data/memory/costs.db"（相对 CWD），
        # 与 config 的 data_dir 无关 → 多个 app 实例共享同一个 DB（包括
        # 跨测试、跨用户运行），导致：
        #   1. 测试间 budget 状态互相污染（前次跑的 cost 累积到下次）
        #   2. 用户重启 app 后 budget 状态依然存在（一次超预算就永久锁死）
        # 修复：默认放到 {data_dir}/memory/costs.db，与其它持久化数据共用
        # data_dir；只有当用户显式指定 db_path 时才用绝对路径覆盖。
        cost_cfg = (ctx.config.get("llm") or {}).get("cost_tracking") or {}
        if cost_cfg:
            data_dir = ctx.config.get("agent", {}).get("data_dir", "./data")
            db_path = cost_cfg.get("db_path") or os.path.join(data_dir, "memory", "costs.db")
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
        #
        # 分阶段超时：connect 短（快速发现不可达），read 长（LLM 生成慢）。
        # 之前用单一 60s 标量，连接阶段也要等满 60s 才报错 →
        # 网络中断时整个 turn 卡死。
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,       # 连接阶段 10s：不可达主机快速失败
                read=float(self._timeout),  # 读取阶段：容忍慢响应
                write=15.0,         # 写入阶段 15s：发送请求体
                pool=5.0,           # 连接池等待 5s
            ),
            limits=httpx.Limits(
                max_connections=MAX_CONNECTIONS,
                max_keepalive_connections=MAX_KEEPALIVE_CONNECTIONS,
                keepalive_expiry=30.0,  # 空闲连接 30s 后回收，避免用已断开的 keep-alive 连接（Errno 103）
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
                # 修复：Python 3.12+ 起 asyncio.get_event_loop() 在同步
                # 上下文里 DeprecationWarning，3.16 移除。直接用
                # new_event_loop + run_until_complete + close。
                # 之所以不尝试 get_running_loop，是因为 _spawn_bg 已经
                # 试过了——它返回 None 就意味着当前不在 async 上下文。
                try:
                    new_loop = _asyncio.new_event_loop()
                    try:
                        new_loop.run_until_complete(coro)
                        ran = True
                    finally:
                        new_loop.close()
                except (RuntimeError, OSError) as exc:
                    # Final safety net: avoid "coroutine was never awaited"
                    logger.error("auto-classify setup failed: %s", exc, exc_info=True)
                    try:
                        coro.close()
                    except (RuntimeError, AttributeError):
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
        if base and api_key and "${" not in (api_key or ""):
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

    def model_supports_tools(self, model: str) -> bool:
        """Check whether a model likely supports tool/function calling.

        Combines explicit cache (_no_tools_models) with heuristic
        pattern matching so flash/lite/small models are detected on the
        very first turn — no wasted failed round-trip needed.

        Also includes a whitelist (_tools_models) for models that contain
        "-flash" or "-lite" but actually do support tools (e.g. deepseek-v4-flash).
        The whitelist is checked FIRST to override any previous negative cache.

        Args:
            model: Full model string like "sensenova/deepseek-v4-flash"
                   or bare model name like "deepseek-v4-flash".

        Returns:
            True if the model probably supports tool calling,
            False if it probably doesn't.
        """
        bare = model.split("/", 1)[1] if "/" in model else model
        if bare in getattr(self, "_tools_models", set()):
            return True
        if bare in self._no_tools_models:
            return False
        for pat in self._no_tools_patterns:
            if pat.search(bare):
                return False
        return True

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
        """True if ``provider`` has a non-empty, unexpanded key configured.

        Also returns True if the provider routes through OpenRouter (i.e. no
        dedicated base URL) and OpenRouter itself has a usable key.
        已被 401 拒绝的 provider（key 失效）返回 False，避免重复试坏 key。
        """
        if provider in self._invalid_keys:
            return False
        v = self._api_keys.get(provider)
        if v and v.strip() and "${" not in v:
            return True
        base = self._provider_base_urls.get(provider)
        openrouter_base = self._provider_base_urls.get("openrouter")
        if base and openrouter_base and base.rstrip("/") == openrouter_base.rstrip("/"):
            if "openrouter" in self._invalid_keys:
                return False
            or_v = self._api_keys.get("openrouter")
            if or_v and or_v.strip() and "${" not in or_v:
                return True
        return False

    def set_api_key(self, provider: str, key: str) -> Dict[str, Any]:
        """Add or update an API key.  Triggers a background reclassify so
        newly-reachable models get slotted into MODEL_TIERS without
        requiring a restart."""
        key = (key or "").strip()
        self._api_keys[provider] = key
        # 用户更新了 key，清除之前的 401 失效标记
        self._invalid_keys.discard(provider)
        # Make sure we have a base URL we can talk to
        if provider not in self._provider_base_urls:
            # Use the resolver if available, otherwise just leave it —
            # get_catalog() / rebuild_tiers() will return no_api_key in that case
            #
            # 安全修复：必须把 api_key 传给 resolve()。原实现调用
            # resolve(provider) 不带 api_key，导致 _probe_one 收到空 key
            # → 不发 Authorization 头 → 任何需要认证的 /models 端点
            # 返回 401/403 → 探测失败 → 未知 provider 永远无法自动发现
            # base_url。已知 provider（在 KNOWN_PROVIDERS 内）不受影响，
            # 但 _add_models 的「自动探测」场景几乎全失败。
            try:
                import asyncio as _asyncio

                from .resolver import resolve
                # 修复：用 get_running_loop() 替代 get_event_loop()。
                # set_api_key 是同步函数，但用户可能在 async 上下文里
                # 调它。在 running loop 里就用 ensure_future 异步调度，
                # 否则用 new_event_loop 一次性跑。
                try:
                    _asyncio.get_running_loop()
                    hint_info = _asyncio.ensure_future(resolve(provider, api_key=key))
                    hint_info.add_done_callback(
                        lambda fut: self._on_provider_resolved(provider, fut.result())
                    )
                except RuntimeError:
                    # No running loop — schedule via new_event_loop for the
                    # background resolve, then register the URL
                    try:
                        new_loop = _asyncio.new_event_loop()
                        try:
                            hint_info = new_loop.run_until_complete(resolve(provider, api_key=key))
                        finally:
                            new_loop.close()
                        self._on_provider_resolved(provider, hint_info)
                    except (RuntimeError, OSError) as exc:
                        logger.error("set_api_key: failed to resolve provider %s: %s", provider, exc, exc_info=True)
            except ImportError as exc:
                logger.error("set_api_key: resolver module missing for %s: %s", provider, exc)
        # Fire-and-forget background reclassify
        ran = False
        import asyncio as _asyncio
        coro = self._auto_classify_one(provider)
        task = self._spawn_bg(coro)
        if task is not None:
            ran = True
        else:
            # 修复：Python 3.12+ 起 get_event_loop() 在同步上下文里
            # DeprecationWarning。_spawn_bg 返回 None 就意味着当前没在
            # async 上下文里，所以直接用 new_event_loop 一次性跑。
            try:
                new_loop = _asyncio.new_event_loop()
                try:
                    new_loop.run_until_complete(coro)
                    ran = True
                finally:
                    new_loop.close()
            except (RuntimeError, OSError) as exc:
                logger.error("set_api_key: failed to run auto-classify for %s: %s", provider, exc, exc_info=True)
                try:
                    coro.close()
                except (RuntimeError, AttributeError):
                    pass
        return {"ok": True, "provider": provider, "key_set": bool(key), "reclassified": ran}

    def _on_provider_resolved(self, provider: str, hint_info: Any) -> None:
        """Callback when an async resolve() finishes — register the URL."""
        if hint_info is None:
            return
        try:
            if hint_info.found and hint_info.base_url:
                self._provider_base_urls[provider] = hint_info.base_url
        except AttributeError as exc:
            logger.error("_on_provider_resolved: invalid hint_info for %s: %s", provider, exc, exc_info=True)

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
        # 修复：Python 3.12+ 起 get_event_loop() 在同步上下文里
        # DeprecationWarning，3.16 移除。这里只在 running loop 里调度；
        # 没 running loop 时返回 None 让调用方走 new_event_loop 兜底。
        try:
            loop = _asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("_spawn_bg: no running event loop, auto-classify deferred")
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
                # 修复：_spawn_bg 返回 None 意味着没 running loop，
                # 直接用 new_event_loop 一次性跑（不再尝试 get_event_loop，
                # 因为 Python 3.12+ 已 DeprecationWarning）。
                try:
                    new_loop = _asyncio.new_event_loop()
                    try:
                        new_loop.run_until_complete(coro)
                    finally:
                        new_loop.close()
                except (RuntimeError, OSError) as exc:
                    logger.error("get_catalog: deferred auto-classify failed: %s", exc, exc_info=True)
                    try:
                        coro.close()
                    except (RuntimeError, AttributeError):
                        pass
        return ModelCatalog(base_url=base, api_key=api_key, provider=prov)

    def _infer_primary_provider(self) -> str:
        # 优先使用 config 显式声明的 primary_provider。
        # 之前只从 _default_model 名字推断 provider（split "/")[0]），
        # 但很多 provider 的 model id 是 "<vendor>/<model>" 格式
        # （如 nvidia 的 "meta/llama-3.1-8b-instruct"），
        # 会被误判为 provider="meta"，导致 base_url 查找错误。
        # 修复：config 的 primary_provider 是权威来源，model 名字只作 fallback。
        if getattr(self, "_primary_provider", None):
            return self._primary_provider
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
            from .resolver import _PROBE_PATH_PATTERNS as _patterns
            from .resolver import _candidate_hosts as _hosts
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

    def _record_cost(
        self,
        model: str,
        tokens_used: int,
        tokens_prompt: int = 0,
        tokens_completion: int = 0,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Accumulate cost totals and persist to the cost tracker.

        Consolidates the 6 duplicated cost-tracking blocks that previously
        appeared in chat_completion / chat_completion_stream. The SQLite
        write is best-effort and never raises to the caller.
        """
        cost = MODEL_COST.get(model, 0.001) * (tokens_used / 1000)
        self._cost_total += cost
        if result is not None:
            result["estimated_cost_usd"] = round(cost, 6)
            result["total_cost_usd"] = round(self._cost_total, 6)
        if self._cost_tracker:
            try:
                provider_name, bare = self._parse_model(model)
                cost_tracked = self._cost_tracker.record(
                    provider=provider_name,
                    model=bare,
                    tokens_prompt=tokens_prompt,
                    tokens_completion=tokens_completion,
                )
                if result is not None:
                    result["cost_usd"] = cost_tracked
            except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
                logger.error("cost_tracker record failed: %s", exc, exc_info=True)

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
        _skip_fallback: bool = False,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Call the LLM, with optional caching and automatic retries.

        Args:
            _skip_fallback: Internal flag to prevent recursive fallback attempts.
            response_format: Optional structured output format, e.g.
                {"type": "json_object"} to force JSON output.
                Only supported by OpenAI-compatible providers.
        """
        assert isinstance(messages, list), "messages must be a list"
        assert len(messages) > 0, "messages cannot be empty"
        assert temperature is None or (0.0 <= temperature <= 2.0), "temperature must be between 0 and 2"
        assert max_tokens is None or max_tokens > 0, "max_tokens must be positive"

        model = model or self._default_model
        temperature = self._default_temperature if temperature is None else temperature
        max_tokens = self._default_max_tokens if max_tokens is None else max_tokens

        # Try cache first.
        # tools + temperature are both included in cache key, so different
        # configs don't share entries.  For stateful tools (weather, API calls)
        # callers should pass use_cache=False explicitly.
        if use_cache and self._cache is not None:
            # 缓存键一致性：get 和 set 必须用相同的 tools 规范化。
            # 之前 set 用 `tools or []`（None → []），get 直接传 tools（None），
            # 导致 tools=None 调用永远 miss（None 生成 "tools":null，[] 生成 "tools":[]）。
            cached = self._cache.get(messages, model, tools or [], temperature)
            if cached is not None:
                logger.info("cache hit for model=%s (tools=%s)", model, bool(tools))
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

        # 推断 provider：之前只从 model 名字 split "/")[0] 推断，但很多
        # provider 的 model id 是 "<vendor>/<model>" 格式（如 nvidia 的
        # "meta/llama-3.1-8b-instruct"），会被误判为 provider="meta"，
        # 导致 base_url 和 api_key 查找失败。
        # 修复：如果 model 的第一段是已注册的 provider（在 _provider_base_urls
        # 里有对应 URL），用它；否则用 config 显式声明的 _primary_provider。
        _first_seg = model.split("/", 1)[0] if "/" in model else ""
        if _first_seg and _first_seg in self._provider_base_urls:
            provider = _first_seg
        elif getattr(self, "_primary_provider", None):
            provider = self._primary_provider
        else:
            provider = _first_seg or "openai"
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
        #
        # 修复 bug：之前无条件 strip 第一段，但对 nvidia 等 provider，
        # model id 是 "<vendor>/<model>" 格式（如 "meta/llama-3.1-8b-instruct"），
        # API 期望完整的 "meta/llama-3.1-8b-instruct"，strip 后的 "llama-3.1-8b-instruct"
        # 会 404。修复：只有当第一段就是 provider 本身时才 strip
        # （如 "openai/gpt-4o" → "gpt-4o"，"nvidia/nemotron-70b" → "nemotron-70b"）。
        # 如果第一段不是 provider（vendor/model 格式），保留完整 id。
        if "/" in model:
            first_seg = model.split("/", 1)[0]
            if first_seg == provider:
                # "provider/model" 格式 → strip provider 前缀
                bare_model = model.split("/", 1)[1]
            else:
                # "vendor/model" 格式（provider≠vendor）→ 保留完整 id
                bare_model = model
        else:
            bare_model = model

        # If no API key is available, fail fast — 但先尝试 fallback chain，
        # 这样当 primary_model 默认到一个未配置的 provider（如 Pydantic
        # 默认的 anthropic）而用户只配了 sensenova 时，能回退到可用 provider，
        # 而不是直接返回 no_api_key 错误。
        if not api_key:
            if not _skip_fallback and self._fallback_chain:
                logger.warning(
                    "no API key for provider %s (model=%s); trying fallback chain",
                    provider, model,
                )
                for fb in self._fallback_chain:
                    fb_model = fb.get("model")
                    if not fb_model or fb_model == model:
                        continue
                    try:
                        result = await self.chat_completion(
                            messages=messages, model=fb_model,
                            temperature=temperature, max_tokens=max_tokens,
                            tools=tools, use_cache=use_cache,
                            _skip_fallback=True,
                        )
                        if not result.get("failed"):
                            result["fallback_used"] = fb_model
                            logger.info("Fallback succeeded with %s (no key for primary %s)", fb_model, provider)
                            return result
                    except Exception as exc:
                        logger.warning("Fallback %s failed: %s", fb_model, exc)
                        continue
            from i18n import _
            return {
                "text": _("no_api_key", provider=provider),
                "tool_calls": [],
                "tool_calls_raw": [],
                "tokens_used": 0,
                "model": model,
                "failed": True,
            }

        # If this model is known to NOT support tool calling,
        # skip sending tools entirely — saves a failed round-trip.
        if tools and not self.model_supports_tools(model):
            logger.debug("model %s known to not support tools, stripping", bare_model)
            tools = None

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
        except (ValueError, KeyError, httpx.RequestError, httpx.TimeoutException) as exc:
            logger.warning("auto-heal model name normalization failed for %s: %s", provider, exc, exc_info=True)
        except Exception as exc:
            logger.warning("auto-heal model name normalization failed with unexpected error for %s: %s", provider, exc, exc_info=True)

        # --- Circuit breaker check ---
        # Get or create circuit breaker for this provider
        if provider not in self._circuit_breakers:
            self._circuit_breakers[provider] = CircuitBreaker()
        circuit_breaker = self._circuit_breakers[provider]

        if not circuit_breaker.can_execute():
            logger.warning(
                "Circuit breaker OPEN for provider %s, skipping call",
                provider
            )
            # Don't return immediately — break to the fallback chain below
            # so an alternative provider can handle the request. Returning
            # here would skip the fallback logic, defeating its purpose.
            # Skip directly to fallback chain
            if not _skip_fallback and self._fallback_chain:
                for fb in self._fallback_chain:
                    fb_model = fb.get("model")
                    if not fb_model or fb_model == model:
                        continue
                    logger.info("circuit open, trying fallback model %s", fb_model)
                    fb_result = await self.chat_completion(
                        messages, model=fb_model, temperature=temperature,
                        max_tokens=max_tokens, tools=tools,
                        use_cache=use_cache, _skip_fallback=True,
                    )
                    if not fb_result.get("failed"):
                        return fb_result
            return {
                "text": f"服务暂时不可用（{provider}），请稍后重试",
                "tool_calls": [],
                "tool_calls_raw": [],
                "tokens_used": 0,
                "model": model,
                "failed": True,
                "circuit_breaker_open": True,
            }

        for attempt in range(1, self._retry_count + 1):
            try:
                result = await self._do_call(
                    base=base, api_key=api_key, model=model,
                    messages=messages, temperature=temperature,
                    max_tokens=max_tokens, tools=tools, provider=provider,
                    response_format=response_format,
                )

                # Record success in circuit breaker
                circuit_breaker.record_success()

                # Record cost
                self._record_cost(
                    model,
                    result.get("tokens_used", 0),
                    tokens_prompt=result.get("tokens_prompt", 0),
                    tokens_completion=result.get("tokens_completion", 0),
                    result=result,
                )

                # Store in cache (tools included in key; stateful tools should use use_cache=False)
                if use_cache and self._cache is not None:
                    self._cache.set(messages, model, tools or [], result, temperature)

                return result
            except (httpx.RequestError, httpx.TimeoutException, httpx.HTTPStatusError, asyncio.TimeoutError, ConnectionError) as exc:
                # Classify: non-retryable errors (4xx auth/bad-request) exit
                # immediately to avoid wasting time on invalid requests.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = (
                    status is None
                    or status in _RETRYABLE_STATUS
                    or isinstance(exc, (asyncio.TimeoutError, ConnectionError))
                )

                # Only record circuit-breaker failures for retryable errors
                # (server-side / network issues). Non-retryable 4xx errors
                # (400 bad request, 401 unauthorized) are client-side problems
                # and should NOT trip the breaker — otherwise repeated bad
                # requests would block the provider for all callers.
                if retryable:
                    circuit_breaker.record_failure()

                if not retryable:
                    # --- Auto-heal: tools not supported (400) ---
                    # Some providers/models don't support function calling.
                    # If we sent tools and got 400, retry without tools.
                    if status == 400 and tools:
                        logger.info("tools not supported by %s, retrying without tools", provider)
                        # Clean messages: strip tool role and tool_calls fields
                        # that some providers reject when tools aren't enabled.
                        clean_msgs = []
                        for m in messages:
                            role = m.get("role", "")
                            if role == "tool":
                                continue
                            if role == "assistant" and m.get("tool_calls"):
                                clean_msg = {k: v for k, v in m.items() if k != "tool_calls"}
                                clean_msgs.append(clean_msg)
                            else:
                                clean_msgs.append(m)
                        try:
                            result = await self._do_call(
                                base=base, api_key=api_key, model=model,
                                messages=clean_msgs, temperature=temperature,
                                max_tokens=max_tokens, tools=None, provider=provider,
                                response_format=response_format,
                            )
                            # Remember: this model doesn't support tools
                            # Skip if in whitelist (don't cache false negatives)
                            if bare_model not in getattr(self, "_tools_models", set()):
                                self._no_tools_models.add(bare_model)
                            self._record_cost(
                                model,
                                result.get("tokens_used", 0),
                                tokens_prompt=result.get("tokens_prompt", 0),
                                tokens_completion=result.get("tokens_completion", 0),
                                result=result,
                            )
                            if use_cache and self._cache is not None:
                                self._cache.set(clean_msgs, model, [], result, temperature)
                            return result
                        except (httpx.RequestError, httpx.TimeoutException, httpx.HTTPStatusError) as retry_exc:
                            retry_status = getattr(getattr(retry_exc, "response", None), "status_code", None)
                            retry_body = ""
                            try:
                                resp_obj = getattr(retry_exc, "response", None)
                                if resp_obj and hasattr(resp_obj, "text"):
                                    retry_body = resp_obj.text[:300]
                            except AttributeError as exc:
                                logger.error("failed to extract retry response body: %s", exc, exc_info=True)
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
                                        response_format=response_format,
                                    )
                                    # Remember: this model doesn't support tools
                                    # Skip if in whitelist (don't cache false negatives)
                                    if bare_model not in getattr(self, "_tools_models", set()):
                                        self._no_tools_models.add(bare_model)
                                    self._record_cost(
                                        model,
                                        result.get("tokens_used", 0),
                                        tokens_prompt=result.get("tokens_prompt", 0),
                                        tokens_completion=result.get("tokens_completion", 0),
                                        result=result,
                                    )
                                    logger.info("last resort succeeded")
                                    return result
                                except (httpx.RequestError, httpx.TimeoutException, httpx.HTTPStatusError) as last_exc:
                                    logger.error("last resort failed: %s", last_exc, exc_info=True)

                    # --- Auto-heal: endpoint fallback ---
                    # When we get 403/404, try probing alternative URLs.
                    # The resolver module has 40+ provider aliases and
                    # candidate host patterns — this is what makes
                    # "give a provider name + key → auto-adapt" work.
                    #
                    # 修复 bug：之前对所有 403/404 都触发 _try_endpoint_fallback，
                    # 但 404 更常见的原因是 "model 不存在/无权限"（base_url 是对的，
                    # /models 能拉到）。此时 fallback 探测 77 个候选 URL 浪费 16s
                    # 且无济于事。修复：404 时先检查 /models 是否可访问，如果可访问
                    # 说明 base_url 对，404 是 model 问题，不触发 fallback。
                    if status in (403, 404) and self._fallback_count.get(provider, 0) < 2:
                        should_fallback = True
                        if status == 404:
                            # /models 可访问 → base_url 对，404 是 model 问题
                            mapping = self._model_name_cache.get(provider, {})
                            if mapping:
                                should_fallback = False
                                logger.info(
                                    "404 for model %s on %s, but /models reachable "
                                    "(%d models) — model issue, not endpoint issue, skipping fallback",
                                    bare_model, provider, len(mapping),
                                )
                        if should_fallback:
                            new_base = await self._try_endpoint_fallback(provider)
                        else:
                            new_base = None
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
                            except (ValueError, KeyError, httpx.RequestError, httpx.TimeoutException) as exc:
                                logger.error("failed to rebuild model name map after fallback: %s", exc, exc_info=True)
                            # Retry once with the new endpoint
                            try:
                                result = await self._do_call(
                                    base=base, api_key=api_key, model=model,
                                    messages=messages, temperature=temperature,
                                    max_tokens=max_tokens, tools=tools, provider=provider,
                                    response_format=response_format,
                                )
                                self._record_cost(
                                    model,
                                    result.get("tokens_used", 0),
                                    tokens_prompt=result.get("tokens_prompt", 0),
                                    tokens_completion=result.get("tokens_completion", 0),
                                    result=result,
                                )
                                if use_cache and self._cache is not None:
                                    self._cache.set(messages, model, tools or [], result, temperature)
                                return result
                            except (httpx.RequestError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                                logger.error("fallback retry failed: %s", exc, exc_info=True)
                    # --- 401: 记忆 key 失效，避免后续请求重复拿坏 key 试 ---
                    if status == 401:
                        self._invalid_keys.add(provider)
                        logger.warning(
                            "llm: provider %s key 返回 401，已标记失效 "
                            "（set_api_key 更新后恢复）", provider,
                        )
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

        # --- Fallback chain: try alternative providers if primary fails ---
        if not _skip_fallback and self._fallback_chain:
            for fb in self._fallback_chain:
                fb_model = fb.get("model")
                if not fb_model or fb_model == model:
                    continue

                logger.info("Trying fallback model: %s", fb_model)
                try:
                    result = await self.chat_completion(
                        messages=messages,
                        model=fb_model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        tools=tools,
                        use_cache=use_cache,
                        _skip_fallback=True,  # Prevent recursive fallback
                    )
                    if not result.get("failed"):
                        result["fallback_used"] = fb_model
                        logger.info("Fallback succeeded with %s", fb_model)
                        return result
                except Exception as exc:
                    logger.warning("Fallback %s failed: %s", fb_model, exc)
                    continue

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

        # 修复：stream 入口加边界校验 (非流式版在 881-882 行有, 之前 stream 缺失)
        # max_tokens=0 传给某些 provider 会 400; temperature>2.0 触发 422
        # 流式失败回退非流式时, 非流式 assert 又会让回退失败 → 死循环
        assert temperature is None or (0.0 <= temperature <= 2.0), "temperature must be between 0 and 2"
        assert max_tokens is None or max_tokens > 0, "max_tokens must be positive"

        # --- Budget check (mirror non-streaming path) ---
        # Without this, streaming callers could bypass the daily/monthly
        # budget cap and keep using expensive models indefinitely.
        if self._cost_tracker:
            budget = self._cost_tracker.check_budget()
            if budget["overall_exceeded"]:
                logger.warning(
                    "Budget exceeded in stream path (daily=$%.4f/%.2f), downgrading",
                    budget["daily"]["cost"], budget["daily"]["budget"],
                )
                model = self._find_cheapest_free_model()

        provider = model.split("/", 1)[0] if "/" in model else "openai"
        base = self._provider_base_urls.get(
            provider,
            self._provider_base_urls.get("openrouter", "https://openrouter.ai/api/v1"),
        )
        api_key = self._api_keys.get(provider) or self._api_keys.get("openrouter")

        # 修复：对于走 openrouter 代理的模型（如 nvidia/xxx），需要保留完整格式，
        # 否则 openrouter 不知道代理哪个 vendor 的模型。
        # 只有当第一段就是 provider 本身时才 strip（如 openai/gpt-4o → gpt-4o）。
        if "/" in model:
            first_seg = model.split("/", 1)[0]
            if first_seg == provider:
                bare_model = model.split("/", 1)[1]
            else:
                bare_model = model
        else:
            bare_model = model

        if not api_key:
            yield {"delta": "", "done": True, "error": f"no API key for provider '{provider}'"}
            return

        # If this model is known to NOT support tool calling, skip sending tools.
        if tools and not self.model_supports_tools(model):
            logger.debug("stream: model %s known to not support tools, stripping", bare_model)
            tools = None

        # --- Circuit breaker check (mirror non-streaming path) ---
        # Without this, streaming callers keep hitting a provider that is
        # already in OPEN state, preventing recovery.
        if provider not in self._circuit_breakers:
            self._circuit_breakers[provider] = CircuitBreaker()
        circuit_breaker = self._circuit_breakers[provider]
        if not circuit_breaker.can_execute():
            logger.warning("Circuit breaker OPEN for provider %s in stream path", provider)
            yield {
                "delta": "",
                "done": True,
                "error": f"服务暂时不可用（{provider}），请稍后重试",
                "circuit_breaker_open": True,
            }
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
                            response_format=None,
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
                            # message_delta carries usage — this is the true
                            # completion signal. Return immediately to avoid
                            # yielding multiple done events (message_stop +
                            # final yield would produce up to 3 done chunks).
                            usage = (data.get("usage") or {})
                            tokens_used = usage.get("output_tokens", 0)
                            circuit_breaker.record_success()
                            self._call_stats.append({"model": model, "tokens_used": tokens_used, "t": time.time()})
                            # Record cost (mirror non-streaming path)
                            self._record_cost(
                                model,
                                tokens_used,
                                tokens_prompt=usage.get("input_tokens", 0),
                                tokens_completion=tokens_used,
                            )
                            yield {"delta": "", "done": True, "tokens_used": tokens_used}
                            return
                    # Stream ended without a message_delta event — yield a
                    # final done with zero tokens as fallback.
                    circuit_breaker.record_success()
                    self._call_stats.append({"model": model, "tokens_used": 0, "t": time.time()})
                    yield {"delta": "", "done": True, "tokens_used": 0}
            except httpx.HTTPStatusError as exc:
                circuit_breaker.record_failure()
                yield {"delta": "", "done": True, "error": f"stream error: {exc.response.status_code}"}
                return
            except (httpx.RequestError, httpx.TimeoutException) as exc:
                circuit_breaker.record_failure()
                logger.error("stream error for anthropic %s: %s", bare_model, exc, exc_info=True)
                yield {"delta": "", "done": True, "error": str(exc)}
                return
            except Exception as exc:
                circuit_breaker.record_failure()
                logger.error("stream error for anthropic %s with unexpected error: %s", bare_model, exc, exc_info=True)
                raise
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
                            response_format=None,
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
                            circuit_breaker.record_success()
                            self._call_stats.append({"model": model, "tokens_used": tokens_used, "t": time.time()})
                            # Record cost (mirror non-streaming path)
                            self._record_cost(
                                model,
                                tokens_used,
                                tokens_prompt=usage.get("prompt_tokens", 0),
                                tokens_completion=usage.get("completion_tokens", 0),
                            )
                            yield {"delta": "", "done": True, "tokens_used": tokens_used}
                            return
                    # Stream ended without a usage chunk — yield final done.
                    circuit_breaker.record_success()
                    self._call_stats.append({"model": model, "tokens_used": 0, "t": time.time()})
                    yield {"delta": "", "done": True, "tokens_used": 0}
            except httpx.HTTPStatusError as exc:
                circuit_breaker.record_failure()
                yield {"delta": "", "done": True, "error": f"stream error: {exc.response.status_code}"}
                return
            except (httpx.RequestError, httpx.TimeoutException) as exc:
                circuit_breaker.record_failure()
                logger.error("stream error for %s/%s: %s", provider, bare_model, exc, exc_info=True)
                yield {"delta": "", "done": True, "error": str(exc)}
                return
            except Exception as exc:
                circuit_breaker.record_failure()
                logger.error("stream error for %s/%s with unexpected error: %s", provider, bare_model, exc, exc_info=True)
                raise

    def stats(self) -> Dict[str, Any]:
        total = len(self._call_stats)
        tokens = sum(c["tokens_used"] for c in self._call_stats)
        return {
            "calls": total,
            "tokens_used": tokens,
            "total_cost_usd": round(self._cost_total, 6),
            "cache": self._cache.stats() if self._cache else {},
            "recent": list(self._call_stats)[-30:],
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
        response_format: Optional[Dict[str, Any]] = None,
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
            # Anthropic Messages API usage only contains input_tokens and
            # output_tokens (no total_tokens field). Compute the sum so
            # tokens_used / cost estimates are accurate.
            usage = data.get("usage") or {}
            tokens_prompt = usage.get("input_tokens", 0)
            tokens_completion = usage.get("output_tokens", 0)
            tokens_used = tokens_prompt + tokens_completion
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
            return result

        # Default — OpenAI compatible
        # Strip the "<provider>/" prefix from the model id — OpenAI-compatible
        # endpoints expect the bare model name (e.g. "deepseek-v4-flash",
        # not "sensenova/deepseek-v4-flash").
        # 修复 bug：和 chat_completion 保持一致的 strip 逻辑——只有当
        # 第一段是 provider 本身时才 strip（"provider/model" → "model"），
        # 否则保留完整 id（"vendor/model" 格式，如 nvidia 的 "meta/llama-3.1-8b-instruct"）。
        if "/" in model:
            _fs = model.split("/", 1)[0]
            bare_model = model.split("/", 1)[1] if _fs == provider else model
        else:
            bare_model = model
        payload = {
            "model": bare_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        # Gap 7 修复：结构化输出支持
        if response_format:
            payload["response_format"] = response_format
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
        return {
            "text": text.strip(),
            "tool_calls": tool_calls,
            "tool_calls_raw": tool_calls_raw,
            "tokens_used": tokens_used,
            "tokens_prompt": tokens_prompt,
            "tokens_completion": tokens_completion,
            "model": data.get("model", model),
        }
