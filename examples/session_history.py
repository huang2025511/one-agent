"""One-Agent 会话历史示例。

演示如何获取和清除会话历史。

运行方式:
    python examples/session_history.py
"""

import asyncio

import httpx

BASE = "http://127.0.0.1:18792"
SESSION = "demo-session"


async def chat(message: str) -> str:
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{BASE}/api/chat",
            json={"message": message, "session_id": SESSION},
        )
        resp.raise_for_status()
        return resp.json().get("reply", "")


async def get_history() -> list:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{BASE}/api/sessions/{SESSION}/messages")
        resp.raise_for_status()
        return resp.json().get("messages", [])


async def main():
    # 发送两条消息
    await chat("我叫小明")
    await chat("我叫什么名字？")

    # 获取历史
    history = await get_history()
    print(f"会话历史 ({len(history)} 条消息):")
    for msg in history:
        role = msg.get("role", "?")
        text = msg.get("content", "")[:80]
        print(f"  [{role}] {text}")


if __name__ == "__main__":
    asyncio.run(main())
