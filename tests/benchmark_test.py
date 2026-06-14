"""Performance benchmark tests — verify system can handle concurrent load."""
import asyncio
import json
import os
import sys
import time
import urllib.request as urlreq

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from one_agent import OneAgentApp


def _post(url: str, data: bytes, timeout: int = 10):
    req = urlreq.Request(url, data=data, headers={"Content-Type": "application/json"})
    r = urlreq.urlopen(req, timeout=timeout)
    body = r.read().decode()
    return r.status, body


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
async def app():
    """Start OneAgentApp for benchmarking."""
    cfg_path = os.environ.get("ONE_AGENT_CONFIG", "config/default_config.yaml")
    application = OneAgentApp(cfg_path)
    application._pm._plugins = [p for p in application._pm._plugins if p.name != "gateway_cli"]
    application.cli = None
    await application.start()
    await asyncio.sleep(2.5)
    yield application
    await application.stop()


@pytest.mark.asyncio
async def test_concurrent_chat_requests(app):
    """Test 50 concurrent chat requests complete successfully."""
    async def send_chat(idx: int):
        data = json.dumps({"text": f"test message {idx}", "session_id": f"bench-{idx}"}).encode()
        try:
            status, body = await asyncio.to_thread(
                _post, "http://127.0.0.1:18792/api/chat", data, 30
            )
            return status == 200
        except Exception:
            return False

    # Launch 50 concurrent requests
    start = time.time()
    tasks = [send_chat(i) for i in range(50)]
    results = await asyncio.gather(*tasks)
    elapsed = time.time() - start

    success_count = sum(1 for r in results if r)
    success_rate = success_count / len(results)

    # At least 90% should succeed under load
    assert success_rate >= 0.90, f"Success rate {success_rate:.2%} below 90%"
    # Should complete within reasonable time (adjust based on actual performance)
    assert elapsed < 60, f"Benchmark took too long: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_rapid_sequential_requests(app):
    """Test 100 rapid sequential requests to verify no resource leaks."""
    success_count = 0
    start = time.time()

    for i in range(100):
        data = json.dumps({"text": f"seq test {i}", "session_id": f"seq-{i}"}).encode()
        try:
            status, _ = await asyncio.to_thread(
                _post, "http://127.0.0.1:18792/api/chat", data, 10
            )
            if status == 200:
                success_count += 1
        except Exception:
            pass

    elapsed = time.time() - start
    success_rate = success_count / 100

    # Should handle sequential requests reliably
    assert success_rate >= 0.95, f"Sequential success rate {success_rate:.2%} below 95%"
    # Average request should complete in reasonable time
    avg_time = elapsed / 100
    assert avg_time < 2.0, f"Average request time {avg_time:.2f}s too slow"


@pytest.mark.asyncio
async def test_memory_search_performance(app):
    """Test memory search endpoint under load."""
    async def search_memory(query: str):
        try:
            status, _ = await asyncio.to_thread(
                urlreq.urlopen,
                f"http://127.0.0.1:18792/api/memory/search?q={query}",
                5
            )
            return status == 200
        except Exception:
            return False

    # 30 concurrent search requests
    start = time.time()
    tasks = [search_memory(f"test{i}") for i in range(30)]
    results = await asyncio.gather(*tasks)
    elapsed = time.time() - start

    success_count = sum(1 for r in results if r)
    success_rate = success_count / len(results)

    assert success_rate >= 0.90
    assert elapsed < 30, f"Memory search benchmark took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_skills_endpoint_performance(app):
    """Test skills list endpoint can handle concurrent access."""
    async def get_skills():
        try:
            status, body = await asyncio.to_thread(
                urlreq.urlopen,
                "http://127.0.0.1:18792/api/skills",
                5
            )
            return status == 200 and "echo" in body
        except Exception:
            return False

    # 50 concurrent requests
    start = time.time()
    tasks = [get_skills() for _ in range(50)]
    results = await asyncio.gather(*tasks)
    elapsed = time.time() - start

    success_count = sum(1 for r in results if r)
    success_rate = success_count / len(results)

    assert success_rate >= 0.95
    assert elapsed < 20, f"Skills endpoint benchmark took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_metrics_endpoint_performance(app):
    """Test metrics endpoint under concurrent load."""
    async def get_metrics():
        try:
            status, body = await asyncio.to_thread(
                urlreq.urlopen,
                "http://127.0.0.1:18792/api/metrics",
                5
            )
            if status != 200:
                return False
            data = json.loads(body)
            return "bus" in data and "llm" in data
        except Exception:
            return False

    # 50 concurrent requests
    start = time.time()
    tasks = [get_metrics() for _ in range(50)]
    results = await asyncio.gather(*tasks)
    elapsed = time.time() - start

    success_count = sum(1 for r in results if r)
    success_rate = success_count / len(results)

    assert success_rate >= 0.95
    assert elapsed < 20, f"Metrics endpoint benchmark took {elapsed:.2f}s"
