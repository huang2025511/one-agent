"""Model recommendation engine — tier classification, rebuild, and recommendation."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from models.tiers import MODEL_TIERS

logger = logging.getLogger(__name__)


class RecommendationMixin:
    """Mixin for LLMProvider providing tier-based model recommendation."""

    def model_for_tier(self, tier: str) -> str:
        """Return the best available model for the given tier.

        Skips providers without a usable API key (expanded, non-empty,
        not a ${VAR} placeholder).  Prioritises models from the default
        model's provider so users get the provider they configured.
        Falls back to _default_model if nothing matches.

        注意：fallback 到 _default_model 时可能跨层（如 expert 列表为空
        但 _default_model 在 complex），这种情况下 tier 路由被破坏。
        修复：跨层 fallback 时记录 warning，方便定位；同时优先尝试相邻
        tier 的可用模型，避免直接跳到 _default_model。

        方案 C：模型存在性验证。auto_classify 后填充 _verified_models，
        遍历模型时验证是否真实存在，避免选中虚构模型（如 sensenova/tiny）。
        """
        default_provider = (
            self._default_model.split("/", 1)[0]
            if "/" in self._default_model
            else None
        )
        candidates = MODEL_TIERS.get(tier, [])

        def _is_real_model(model: str) -> bool:
            """验证模型是否真实存在。
            如果 _verified_models 没有该 provider 的记录（auto_classify
            还没跑或失败），返回 True（不验证，向后兼容）。
            否则检查 bare_model.lower() 是否在真实模型集合里。
            """
            provider, _, bare = model.partition("/")
            verified = self._verified_models.get(provider)
            if not verified:
                return True  # 还没验证过，不阻断
            return bare.lower() in verified

        # 第一优先：default provider 的可用且真实的模型
        if default_provider:
            for model in candidates:
                provider = model.split("/", 1)[0]
                if (provider == default_provider
                        and self._has_usable_key(provider)
                        and _is_real_model(model)):
                    return model
        # 第二优先：tier 内任意 provider 的可用且真实的模型
        for model in candidates:
            provider = model.split("/", 1)[0]
            if (self._has_usable_key(provider) and _is_real_model(model)):
                return model
        # 仅当 _default_model 的 provider 有可用 key 时才 fallback 到它，
        # 否则返回 None 让上游（router）感知路由失败，而非用跨层模型
        # 污染 tier 语义。返回 None 比 _default_model 更安全——
        # _default_model 可能与请求 tier 完全不匹配。
        if default_provider and self._has_usable_key(default_provider):
            logger.warning(
                "model_for_tier: %s 层无可用模型，fallback 到 _default_model %s（可能跨层）",
                tier, self._default_model,
            )
            return self._default_model
        # _default_model 的 provider 也没 key（典型场景：primary_model 默认到
        # anthropic 但用户只配了 sensenova）。此时返回 _default_model 会让
        # chat_completion 直接报 no_api_key——看似合理，但对用户而言是"明明
        # 配了 sensenova 却用不了"。修复：扫描所有 tier，找任意有可用 key 的
        # provider 的模型返回（跨 tier 也比用没 key 的 provider 强，至少能
        # 跑通 LLM 调用）。这样即使用户只配了 sensenova，router 也能选到
        # sensenova/tiny 而非无 key 的 anthropic/claude-3.5-sonnet。
        for any_tier_models in MODEL_TIERS.values():
            for model in any_tier_models:
                prov = model.split("/", 1)[0] if "/" in model else ""
                if prov and prov != default_provider and self._has_usable_key(prov):
                    logger.warning(
                        "model_for_tier: %s 层无可用模型且 _default_model 的 provider "
                        "%s 也无 key，跨 tier 回退到 %s（有可用 key）",
                        tier, default_provider, model,
                    )
                    return model
        # 真的没有任何 provider 有 key：返回 _default_model 让 chat_completion
        # 报 no_api_key（此时 fallback chain 也无济于事，因为没有 provider 有 key）
        return self._default_model

    def _resolve_config_path(self) -> Optional[str]:
        """定位 config 文件路径用于持久化。"""
        # 优先环境变量
        import os
        env_path = os.environ.get("ONE_AGENT_CONFIG")
        if env_path:
            return env_path
        # 从 ctx 推断（data_dir 或 cwd）
        try:
            cfg = getattr(self, "_config", None)
            if cfg and isinstance(cfg, dict):
                # config 中无显式 path 字段，回退到默认 ./config/default_config.yaml
                for cand in ("./config/default_config.yaml", "./default_config.yaml"):
                    import os as _os
                    if _os.path.exists(cand):
                        return cand
        except Exception as exc:
            logger.debug("ignored non-critical error: %s", exc)
        return None

    def _dump_config(self, cfg: Dict[str, Any], path: str) -> None:
        """将 config dict 写回 YAML 文件（原子替换）。

        安全说明：cfg 来自内存，可能已把 ${VAR} 占位符展开成实际值、
        把 enc:xxx 解密成明文 API key。直接写盘会把明文密钥永久固化
        到配置文件，构成密钥泄漏。修复：写盘前递归脱敏——
        - llm.api_keys 下任何非空值还原为 ${ENV_VAR} 占位符（若值与
          某个环境变量相等）或写回 null（无法回溯 env 时）
        - 任何 *_token / *_password / secret 字段同样处理
        - enc: 前缀的值保持原样（加密内容本身安全）

        更彻底的修复是从磁盘读取原始 YAML 再 patch，但当前 rebuild_tiers
        只需持久化 model_tiers，且原始 ${VAR} 不可逆推，故采用脱敏策略。
        """
        import os
        import tempfile
        import yaml
        from pathlib import Path

        # 写盘前脱敏：递归遍历 cfg，把展开的密钥还原为占位符或 null
        sanitized = self._sanitize_for_persist(cfg)

        path_obj = Path(path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        # 原子写入：先写临时文件，再 rename
        fd, tmp_path = tempfile.mkstemp(
            prefix=".config_", suffix=".yaml", dir=str(path_obj.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(sanitized, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            os.replace(tmp_path, path)
        except Exception:
            # 清理临时文件
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # 敏感键名模式（精确小写匹配）——这些字段的值若已被展开成明文，
    # 写盘前必须还原为 ${ENV_VAR} 或 null，避免明文落盘。
    # 注意：password / password_hash 不在内——hash 本身可安全落盘，
    # 明文密码字段不通过 config 文件管理（用户应通过环境变量配置）。
    _SENSITIVE_KEY_NAMES = {
        "api_key", "api_keys", "apikey", "secret", "secret_key",
        "token", "access_token", "refresh_token",
        "private_key", "client_secret",
    }

    def _sanitize_for_persist(self, cfg: Any) -> Any:
        """递归脱敏：把展开的密钥还原为 ${ENV_VAR} 占位符或 null。

        策略：
        1. 对于 dict 的 key 匹配敏感名模式：尝试在 os.environ 中找到
           与当前值相等的变量名，还原为 ${VAR}；找不到则写 null。
        2. enc: 前缀的加密值保留（加密内容本身可安全落盘）。
        3. ${VAR} 占位符保留（本来就是占位符，无需处理）。
        4. dict/list 递归。
        """
        import os
        if isinstance(cfg, dict):
            out = {}
            for k, v in cfg.items():
                key_lower = str(k).lower()
                # llm.api_keys 整体作为敏感容器：值为 dict 时遍历脱敏，
                # 值为 str 时尝试还原
                if key_lower in self._SENSITIVE_KEY_NAMES:
                    out[k] = self._redact_value(v)
                else:
                    out[k] = self._sanitize_for_persist(v)
            return out
        if isinstance(cfg, list):
            return [self._sanitize_for_persist(item) for item in cfg]
        return cfg

    def _redact_value(self, value: Any) -> Any:
        """把单个敏感值还原为 ${ENV_VAR} 或 null。"""
        import os
        if isinstance(value, dict):
            # llm.api_keys 的值通常是 {provider: key_str}
            return {k: self._redact_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        if not isinstance(value, str):
            return value
        # enc: 加密内容保留（本身可安全落盘）
        if value.startswith("enc:"):
            return value
        # ${VAR} 占位符保留
        if value.startswith("${") and value.endswith("}"):
            return value
        # 空值保留
        if not value:
            return value
        # 尝试在 env 中找到与 value 相等的变量名
        for env_name, env_val in os.environ.items():
            if env_val == value and self._is_safe_env_name(env_name):
                return f"${{{env_name}}}"
        # 找不到对应 env var：写 null，避免明文落盘。
        # 用户后续可通过环境变量重新配置。
        return None

    @staticmethod
    def _is_safe_env_name(name: str) -> bool:
        """只把可识别的密钥类 env var 还原为占位符。

        防止把普通 env var（如 PATH=/usr/bin）误还原为 ${PATH}。
        """
        upper = name.upper()
        return any(
            kw in upper for kw in (
                "API_KEY", "APIKEY", "SECRET", "TOKEN", "PASSWORD",
                "PRIVATE_KEY", "CLIENT_SECRET", "ACCESS_TOKEN",
            )
        )

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
        from .catalog import diff_tiers as _diff
        from .catalog import rebuild_tiers as _rebuild
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
            # 填充 _verified_models：把该 provider 真实存在的模型 ID 集合
            # 缓存起来，供 model_for_tier() 验证硬编码模型是否真实存在。
            try:
                real_ids = {m.id.lower() for m in cat.all() if m.id}
                if real_ids:
                    self._verified_models[prov] = real_ids
                    logger.debug("rebuild_tiers: verified %d model IDs for %s",
                                 len(real_ids), prov)
            except Exception as exc:  # noqa: BLE001
                logger.debug("rebuild_tiers: failed to populate _verified_models: %s", exc)
            if persist:
                try:
                    # 之前 self._config 在 LLMProvider 上从未初始化（已在
                    # setup() 中修复），且本块只修改内存 dict 而从未写盘，
                    # 导致 persist=True 与 persist=False 行为完全等价。
                    cfg = getattr(self, "_config", None) or {}
                    if isinstance(cfg, dict):
                        cfg.setdefault("llm", {})["model_tiers"] = {
                            k: list(v) for k, v in new.items()
                        }
                        self._config = cfg
                        # 真正写盘：回写 config 文件
                        config_path = self._resolve_config_path()
                        if config_path:
                            self._dump_config(cfg, config_path)
                except Exception as exc:  # noqa: BLE001
                    # 持久化失败不应阻断 rebuild 主流程（内存 tiers 已更新），
                    # 但需用 warning 级别让运维感知（原 debug 级别会静默吞掉）。
                    logger.warning("rebuild_tiers persist failed: %s", exc, exc_info=True)
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
        from .capabilities import (
            RECOMMEND_CATEGORIES,
            describe_capabilities,
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
