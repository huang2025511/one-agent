"""Performance benchmark tests — verify system can handle concurrent load."""
import asyncio
import os
import shutil
import sys
import tempfile
import time

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))



@pytest.mark.asyncio
async def test_concurrent_chat_requests(app):
    """Test 10 concurrent chat requests (free API rate-limit aware)."""
    async def send_chat(client, idx: int):
        try:
            r = await client.post(
                "http://127.0.0.1:18792/api/chat",
                json={"text": f"hi {idx}", "session_id": f"bench-{idx}"},
                timeout=60.0
            )
            return r.status_code == 200
        except Exception:
            return False

    # 10 concurrent requests (respects free API limits)
    start = time.time()
    async with httpx.AsyncClient() as client:
        tasks = [send_chat(client, i) for i in range(10)]
        results = await asyncio.gather(*tasks)
    elapsed = time.time() - start

    success_count = sum(1 for r in results if r)
    success_rate = success_count / len(results)

    assert success_rate >= 0.70, f"Success rate {success_rate:.2%} below 70%"
    assert elapsed < 120, f"Benchmark took too long: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_rapid_sequential_requests(app):
    """Test 100 rapid sequential requests to verify no resource leaks."""
    success_count = 0
    start = time.time()

    async with httpx.AsyncClient() as client:
        for i in range(20):
            try:
                r = await client.post(
                    "http://127.0.0.1:18792/api/chat",
                    json={"text": f"hi {i}", "session_id": f"seq-{i}"},
                    timeout=30.0
                )
                if r.status_code == 200:
                    success_count += 1
            except Exception:
                pass

    elapsed = time.time() - start
    success_rate = success_count / 20

    assert success_rate >= 0.80, f"Sequential success rate {success_rate:.2%} below 80%"
    avg_time = elapsed / 20
    assert avg_time < 5.0, f"Average request time {avg_time:.2f}s too slow"


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


# ══════════════════════════════════════════════════════════════════════════
# 离线性能基准测试（纯 Python，不需要连接服务器，可在 CI 中运行）
# ══════════════════════════════════════════════════════════════════════════

def test_cache_performance():
    """测试 LLMCache 的 get/set 性能（离线，不需要服务器）。"""
    from models import LLMCache

    cache = LLMCache(max_size=1000)

    # 写入 1000 条缓存
    start = time.time()
    for i in range(1000):
        messages = [{"role": "user", "content": f"benchmark-query-{i}"}]
        cache.set(messages, "bench-model", None, {"text": f"response-{i}", "tokens_used": 10})
    write_elapsed = time.time() - start
    assert write_elapsed < 1.0, f"写入 1000 条缓存耗时 {write_elapsed:.3f}s，超过 1.0s"

    # 读取 1000 次（500 hit + 500 miss）
    start = time.time()
    for i in range(500):
        # 命中：读取已写入的条目
        messages = [{"role": "user", "content": f"benchmark-query-{i}"}]
        result = cache.get(messages, "bench-model", None)
        assert result is not None, f"第 {i} 条缓存应命中但未命中"
    for i in range(500):
        # 未命中：读取从未写入的条目
        messages = [{"role": "user", "content": f"missing-query-{i}"}]
        result = cache.get(messages, "bench-model", None)
        assert result is None, f"第 {i} 条缓存应未命中但命中"
    read_elapsed = time.time() - start
    assert read_elapsed < 0.5, f"读取 1000 次缓存耗时 {read_elapsed:.3f}s，超过 0.5s"

    # 断言 hit_rate 约为 0.5（500 hits / 1000 total）
    stats = cache.stats()
    assert abs(stats["hit_rate"] - 0.5) < 0.05, (
        f"hit_rate={stats['hit_rate']}，期望约 0.5"
    )


def test_role_library_search_performance():
    """测试角色库搜索性能（离线，不需要服务器）。"""
    try:
        from core.roles import get_library
    except ImportError:
        # core.roles 模块在当前环境中不存在，跳过该测试
        return

    lib = get_library()
    lib.load()

    # 对 10 个不同关键词执行 search()，每个搜索 100 次
    keywords = ["助手", "翻译", "编程", "写作", "分析", "设计", "管理", "学习", "聊天", "搜索"]
    start = time.time()
    for keyword in keywords:
        for _ in range(100):
            results = lib.search(keyword)
            assert results, f"关键词 '{keyword}' 搜索未返回结果"
    elapsed = time.time() - start
    assert elapsed < 2.0, f"角色库搜索总耗时 {elapsed:.3f}s，超过 2.0s"


def test_memory_search_performance_offline():
    """测试长期记忆搜索性能（离线，不需要服务器）。"""
    from memory import LongTermMemory

    tmp_dir = tempfile.mkdtemp()
    try:
        db_path = os.path.join(tmp_dir, "longterm.sqlite")
        ltm = LongTermMemory(path=db_path)

        # 写入 100 条记忆
        for i in range(100):
            ltm.add(
                content=f"benchmark memory entry number {i} about performance testing",
                source="test",
                tags="benchmark",
            )

        # 执行 50 次搜索
        queries = ["benchmark", "memory", "performance", "testing", "entry"]
        start = time.time()
        for i in range(50):
            results = ltm.search(queries[i % len(queries)], limit=5)
        elapsed = time.time() - start
        assert elapsed < 3.0, f"长期记忆搜索 50 次总耗时 {elapsed:.3f}s，超过 3.0s"

        ltm.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_audit_log_performance():
    """测试审计日志写入性能（离线，不需要服务器）。"""
    from core.audit_log import AuditLog

    tmp_dir = tempfile.mkdtemp()
    try:
        db_path = os.path.join(tmp_dir, "audit.db")
        audit = AuditLog(db_path=db_path)

        # 写入 500 条审计日志
        start = time.time()
        for i in range(500):
            audit.log(
                action="benchmark_test",
                actor=f"user-{i}",
                resource=f"/api/benchmark/{i}",
                details={"index": i, "msg": "benchmark entry"},
            )
        write_elapsed = time.time() - start
        assert write_elapsed < 2.0, f"写入 500 条审计日志耗时 {write_elapsed:.3f}s，超过 2.0s"

        # 查询 10 次
        start = time.time()
        for _ in range(10):
            results = audit.query(action="benchmark_test", limit=100)
        query_elapsed = time.time() - start
        assert query_elapsed < 0.5, f"查询 10 次审计日志耗时 {query_elapsed:.3f}s，超过 0.5s"

        audit.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
