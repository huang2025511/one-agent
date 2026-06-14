"""Shared test fixtures — single app instance shared across all test files."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from one_agent import OneAgentApp


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def app():
    """Start OneAgentApp once for the entire test session."""
    cfg_path = os.environ.get("ONE_AGENT_CONFIG", "config/default_config.yaml")
    application = OneAgentApp(cfg_path)
    # Remove CLI gateway to avoid input() blocking
    application._pm._plugins = [p for p in application._pm._plugins if p.name != "gateway_cli"]
    application.cli = None
    await application.start()
    await asyncio.sleep(2.5)
    yield application
    await application.stop()
