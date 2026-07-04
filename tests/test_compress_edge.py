"""验证 _compress_context 边界情况：短消息列表不应导致 system 提示词重复。"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.mark.asyncio
async def test_short_message_list_no_duplicate_system():
    """当 keep_recent >= len(messages) 时，原始 system 不应被重复添加。"""
    from core.coordinator import Coordinator

    coord = Coordinator()
    coord._llm = None

    original_system = {"role": "system", "content": "你是 One-Agent"}
    # 4 条消息（触发 keep_recent = max(4, 1) = 4 >= len(messages) = 4）
    messages = [
        original_system,
        {"role": "user", "content": "x" * 5000},
        {"role": "assistant", "content": "y" * 5000},
        {"role": "user", "content": "z" * 5000},
    ]

    async def fake_compress(msgs, turn):
        return "摘要"
    coord._compress_messages = fake_compress

    turn = SimpleNamespace(model="test", meta={}, session_id="t1")
    coord.ctx = SimpleNamespace(config={
        "router": {"context_compression": {"enabled": True}},
        "memory": {"short_term": {"max_tokens": 100}},
    })

    await coord._compress_context(messages, turn)

    # 统计 system 消息数量
    system_messages = [m for m in messages if m.get("role") == "system"]
    n_original = sum(1 for m in system_messages if m.get("content") == "你是 One-Agent")
    assert n_original == 1, \
        f"原始 system 提示词应只出现一次，实际 {n_original} 次。messages={system_messages}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
