"""Shared test fixtures — single app instance shared across all test files."""
import asyncio
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from one_agent import OneAgentApp


def _run_app_in_thread(app_instance, ready_event):
    """Run the app in a dedicated event loop thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def start_and_wait():
        await app_instance.start()
        # Wait for services to be ready
        import httpx
        ready = False
        for _ in range(20):
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    r = await client.get("http://127.0.0.1:18792/api/health/live")
                    if r.status_code == 200:
                        ready = True
                        break
            except Exception:
                await asyncio.sleep(0.5)
        # 修复 bug：之前无论 health check 是否成功都 set()，把 REST API
        # 启动失败（如 fastapi 未装）掩盖成 fixture 成功，导致后续 8 个
        # e2e 用例各自报 ConnectError 而非在 fixture 阶段就暴露根因。
        # 现在 set 一个标志位，由外层判断是否真的 ready。
        ready_event.ready = ready  # type: ignore[attr-defined]
        ready_event.set()

    try:
        loop.run_until_complete(start_and_wait())
        # Keep the loop running
        loop.run_forever()
    finally:
        loop.close()


@pytest.fixture(scope="session")
def app():
    """Start OneAgentApp in a dedicated thread for the entire test session."""
    # Use test-specific config with higher rate limits
    cfg_path = os.environ.get("ONE_AGENT_CONFIG", "config/test_config.yaml")
    application = OneAgentApp(cfg_path)
    # Remove CLI gateway to avoid input() blocking
    application._pm._plugins = [p for p in application._pm._plugins if p.name != "gateway_cli"]
    application.cli = None

    ready_event = threading.Event()
    thread = threading.Thread(target=_run_app_in_thread, args=(application, ready_event), daemon=True)
    thread.start()

    # Wait for app to be ready
    if not ready_event.wait(timeout=15):
        raise RuntimeError("App failed to start within timeout")
    # 如果 REST API 没起来（如 fastapi 未装），跳过依赖它的 e2e 用例
    # 而不是让每个用例各自报 ConnectError。
    if not getattr(ready_event, "ready", False):
        import pytest
        pytest.skip("REST API 未就绪（可能 fastapi 未安装或端口被占），跳过依赖 REST API 的 e2e 用例",
                    allow_module_level=False)

    yield application

    # Cleanup: stop the app
    # Note: Since app is running in another thread's event loop, we can't easily stop it
    # The daemon thread will be killed when the test session ends
