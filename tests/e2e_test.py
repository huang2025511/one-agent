"""End-to-end integration test — starts One-Agent and verifies all services."""
import asyncio
import json
import os
import sys
import urllib.request as urlreq

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from one_agent import OneAgentApp


def _get(url: str, timeout: int = 5):
    r = urlreq.urlopen(url, timeout=timeout)
    body = r.read().decode()
    return r.status, body


def _post(url: str, data: bytes, timeout: int = 10):
    req = urlreq.Request(url, data=data, headers={"Content-Type": "application/json"})
    r = urlreq.urlopen(req, timeout=timeout)
    body = r.read().decode()
    return r.status, body


@pytest.mark.asyncio
async def test_rest_api_health(app):
    status, body = await asyncio.to_thread(_get, "http://127.0.0.1:18792/api/health")
    assert status == 200


@pytest.mark.asyncio
async def test_skills_list(app):
    status, body = await asyncio.to_thread(_get, "http://127.0.0.1:18792/api/skills")
    assert status == 200
    assert "echo" in body and "calc" in body


@pytest.mark.asyncio
async def test_metrics(app):
    status, body = await asyncio.to_thread(_get, "http://127.0.0.1:18792/api/metrics")
    assert status == 200
    data = json.loads(body)
    assert "bus" in data
    assert "llm" in data


@pytest.mark.asyncio
async def test_web_ui(app):
    status, html = await asyncio.to_thread(_get, "http://127.0.0.1:18791/")
    assert status == 200
    assert "One-Agent" in html


@pytest.mark.asyncio
async def test_monitor_dashboard(app):
    status, html = await asyncio.to_thread(_get, "http://127.0.0.1:18793/")
    assert status == 200
    assert "One-Agent" in html


@pytest.mark.asyncio
async def test_memory_search(app):
    status, _ = await asyncio.to_thread(
        _get, "http://127.0.0.1:18792/api/memory/search?q=python"
    )
    assert status == 200


@pytest.mark.asyncio
async def test_chat_endpoint(app):
    data = json.dumps({"text": "hello", "session_id": "test"}).encode()
    status, _ = await asyncio.to_thread(
        _post, "http://127.0.0.1:18792/api/chat", data, 10
    )
    assert status == 200


@pytest.mark.asyncio
async def test_settings_page(app):
    status, _ = await asyncio.to_thread(_get, "http://127.0.0.1:18792/api/settings")
    assert status == 200
