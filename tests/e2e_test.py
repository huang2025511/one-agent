"""End-to-end integration test — starts One-Agent without CLI and verifies all services."""
import asyncio, os, sys, json
import urllib.request as urlreq

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from one_agent import OneAgentApp


def _get(url: str, timeout: int = 5):
    """Synchronous GET — must be called via asyncio.to_thread() to avoid blocking event loop."""
    r = urlreq.urlopen(url, timeout=timeout)
    body = r.read().decode()
    return r.status, body


def _post(url: str, data: bytes, timeout: int = 10):
    """Synchronous POST — must be called via asyncio.to_thread() to avoid blocking event loop."""
    req = urlreq.Request(url, data=data, headers={"Content-Type": "application/json"})
    r = urlreq.urlopen(req, timeout=timeout)
    body = r.read().decode()
    return r.status, body


async def test():
    cfg_path = os.environ.get("ONE_AGENT_CONFIG", "config/default_config.yaml")
    print(f"Loading config: {cfg_path}")
    app = OneAgentApp(cfg_path)

    # Remove CLI gateway to avoid input() blocking
    app._pm._plugins = [p for p in app._pm._plugins if p.name != "gateway_cli"]
    app.cli = None

    await app.start()
    await asyncio.sleep(2.5)  # wait for all servers to be ready

    results = {}

    # --- Test 1: REST API Health ---
    print("=== REST API Health ===")
    try:
        status, body = await asyncio.to_thread(_get, "http://127.0.0.1:18792/api/health")
        print(f"  Status: {status}, Body: {body}")
        results["health"] = status == 200
    except Exception as e:
        print(f"  FAIL: {e}")
        results["health"] = False

    # --- Test 2: Skills List ---
    print("=== Skills List ===")
    try:
        status, body = await asyncio.to_thread(_get, "http://127.0.0.1:18792/api/skills")
        has_skills = "echo" in body and "calc" in body
        print(f"  Status: {status}, Has echo+calc: {has_skills}")
        results["skills"] = status == 200 and has_skills
    except Exception as e:
        print(f"  FAIL: {e}")
        results["skills"] = False

    # --- Test 3: Metrics ---
    print("=== Metrics ===")
    try:
        status, body = await asyncio.to_thread(_get, "http://127.0.0.1:18792/api/metrics")
        data = json.loads(body)
        has_bus = "bus" in data
        has_llm = "llm" in data
        print(f"  Status: {status}, Has bus+llm: {has_bus}+{has_llm}")
        results["metrics"] = status == 200 and has_bus and has_llm
    except Exception as e:
        print(f"  FAIL: {e}")
        results["metrics"] = False

    # --- Test 4: Web UI ---
    print("=== Web UI ===")
    try:
        status, html = await asyncio.to_thread(_get, "http://127.0.0.1:18791/")
        has_title = "One-Agent" in html
        print(f"  Status: {status}, Size: {len(html)}B, Has 'One-Agent': {has_title}")
        results["web"] = status == 200 and has_title
    except Exception as e:
        print(f"  FAIL: {e}")
        results["web"] = False

    # --- Test 5: Monitor Dashboard ---
    print("=== Monitor Dashboard ===")
    try:
        status, html = await asyncio.to_thread(_get, "http://127.0.0.1:18793/")
        has_title = "One-Agent Monitor" in html or "One-Agent" in html
        print(f"  Status: {status}, Size: {len(html)}B, Has 'One-Agent': {has_title}")
        results["monitor"] = status == 200 and has_title
    except Exception as e:
        print(f"  FAIL: {e}")
        results["monitor"] = False

    # --- Test 6: Memory Search ---
    print("=== Memory Search ===")
    try:
        status, body = await asyncio.to_thread(_get, "http://127.0.0.1:18792/api/memory/search?q=python")
        print(f"  Status: {status}, Body: {body[:100]}")
        results["memory"] = status == 200
    except Exception as e:
        print(f"  FAIL: {e}")
        results["memory"] = False

    # --- Test 7: Chat endpoint ---
    print("=== Chat ===")
    try:
        data = json.dumps({"text": "hello", "session_id": "test"}).encode()
        status, body = await asyncio.to_thread(
            _post, "http://127.0.0.1:18792/api/chat", data, 10
        )
        print(f"  Status: {status}, Body: {body[:100]}")
        results["chat"] = status == 200
    except Exception as e:
        print(f"  FAIL: {e}")
        results["chat"] = False

    # --- Test 8: Settings page ---
    print("=== Settings ===")
    try:
        status, body = await asyncio.to_thread(_get, "http://127.0.0.1:18792/api/settings")
        print(f"  Status: {status}, Body: {body[:100]}")
        results["settings"] = status == 200
    except Exception as e:
        print(f"  FAIL: {e}")
        results["settings"] = False

    await app.stop()

    # Summary
    print()
    print("=" * 50)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for name, ok in results.items():
        print(f"  {name:12s}: {'PASS' if ok else 'FAIL'}")
    print(f"  Total: {passed}/{total}")
    print("=" * 50)

    return passed == total


if __name__ == "__main__":
    ok = asyncio.run(test())
    sys.exit(0 if ok else 1)