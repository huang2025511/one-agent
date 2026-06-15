"""End-to-end integration test — starts One-Agent and verifies all services."""
import asyncio
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from one_agent import OneAgentApp


@pytest.mark.asyncio
async def test_rest_api_health(app):
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get("http://127.0.0.1:18792/api/health")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_skills_list(app):
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get("http://127.0.0.1:18792/api/skills")
        assert r.status_code == 200
        body = r.text
        assert "echo" in body and "calc" in body


@pytest.mark.asyncio
async def test_metrics(app):
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get("http://127.0.0.1:18792/api/metrics")
        assert r.status_code == 200
        data = r.json()
        assert "bus" in data
        assert "llm" in data


@pytest.mark.asyncio
async def test_web_ui(app):
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get("http://127.0.0.1:18791/")
        assert r.status_code == 200
        assert "One-Agent" in r.text


@pytest.mark.asyncio
async def test_monitor_dashboard(app):
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get("http://127.0.0.1:18793/")
        assert r.status_code == 200
        assert "One-Agent" in r.text


@pytest.mark.asyncio
async def test_memory_search(app):
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get("http://127.0.0.1:18792/api/memory/search?q=python")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_chat_endpoint(app):
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "http://127.0.0.1:18792/api/chat",
            json={"text": "hello", "session_id": "test"}
        )
        if r.status_code != 200:
            print(f"DEBUG: status={r.status_code} body={r.text}")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_settings_page(app):
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get("http://127.0.0.1:18792/api/settings")
        assert r.status_code == 200
