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
        for _ in range(20):
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    r = await client.get("http://127.0.0.1:18792/api/health/live")
                    if r.status_code == 200:
                        break
            except Exception:
                await asyncio.sleep(0.5)
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

    yield application

    # Cleanup: stop the app
    # Note: Since app is running in another thread's event loop, we can't easily stop it
    # The daemon thread will be killed when the test session ends
