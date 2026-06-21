"""One-Agent 快速开始示例。

运行方式:
    python examples/quickstart.py

前提条件:
    1. 设置 API key:  export SENSENOVA_API_KEY="your-key"
    2. 启动 one-agent: python one_agent.py (另一个终端)
"""

import asyncio

import httpx


async def chat(message: str) -> str:
    """发送一条消息到 One-Agent REST API 并返回回复。"""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "http://127.0.0.1:18792/api/chat",
            json={"message": message, "session_id": "demo"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("reply", data.get("error", {}).get("message", "(no reply)"))


async def main():
    reply = await chat("你好，请介绍一下你自己")
    print(f"One-Agent: {reply}")


if __name__ == "__main__":
    asyncio.run(main())
