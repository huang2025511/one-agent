"""Model recommendation engine — tier classification, rebuild, and recommendation."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from models import MODEL_TIERS

logger = logging.getLogger(__name__)


class RecommendationMixin:
    """Mixin for LLMProvider providing tier-based model recommendation."""

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
