"""Performance benchmark tests — verify system can handle concurrent load."""
import asyncio
import json
import os
import sys
import time

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from one_agent import OneAgentApp


@pytest.mark.asyncio
async def test_concurrent_chat_requests(app):
    """Test 50 concurrent chat requests complete successfully."""
    async def send_chat(client, idx: int):
        try:
            r = await client.post(
                "http://127.0.0.1:18792/api/chat",
                json={"text": f"test message {idx}", "session_id": f"bench-{idx}"},
                timeout=30.0
            )
            return r.status_code == 200
        except Exception:
            return False

    # Launch 50 concurrent requests
    start = time.time()
    async with httpx.AsyncClient() as client:
        tasks = [send_chat(client, i) for i in range(50)]
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

    async with httpx.AsyncClient() as client:
        for i in range(100):
            try:
                r = await client.post(
                    "http://127.0.0.1:18792/api/chat",
                    json={"text": f"seq test {i}", "session_id": f"seq-{i}"},
                    timeout=10.0
                )
                if r.status_code == 200:
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
    async def search_memory(client, query: str):
        try:
            r = await client.get(
                f"http://127.0.0.1:18792/api/memory/search?q={query}",
                timeout=5.0
            )
            return r.status_code == 200
        except Exception:
            return False

    # 30 concurrent search requests
    start = time.time()
    async with httpx.AsyncClient() as client:
        tasks = [search_memory(client, f"test{i}") for i in range(30)]
        results = await asyncio.gather(*tasks)
    elapsed = time.time() - start

    success_count = sum(1 for r in results if r)
    success_rate = success_count / len(results)

    assert success_rate >= 0.90
    assert elapsed < 30, f"Memory search benchmark took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_skills_endpoint_performance(app):
    """Test skills list endpoint can handle concurrent access."""
    async def get_skills(client):
        try:
            r = await client.get("http://127.0.0.1:18792/api/skills", timeout=5.0)
            return r.status_code == 200 and "echo" in r.text
        except Exception:
            return False

    # 50 concurrent requests
    start = time.time()
    async with httpx.AsyncClient() as client:
        tasks = [get_skills(client) for _ in range(50)]
        results = await asyncio.gather(*tasks)
    elapsed = time.time() - start

    success_count = sum(1 for r in results if r)
    success_rate = success_count / len(results)

    assert success_rate >= 0.95
    assert elapsed < 20, f"Skills endpoint benchmark took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_metrics_endpoint_performance(app):
    """Test metrics endpoint under concurrent load."""
    async def get_metrics(client):
        try:
            r = await client.get("http://127.0.0.1:18792/api/metrics", timeout=5.0)
            if r.status_code != 200:
                return False
            data = r.json()
            return "bus" in data and "llm" in data
        except Exception:
            return False

    # 50 concurrent requests
    start = time.time()
    async with httpx.AsyncClient() as client:
        tasks = [get_metrics(client) for _ in range(50)]
        results = await asyncio.gather(*tasks)
    elapsed = time.time() - start

    success_count = sum(1 for r in results if r)
    success_rate = success_count / len(results)

    assert success_rate >= 0.95
    assert elapsed < 20, f"Metrics endpoint benchmark took {elapsed:.2f}s"
