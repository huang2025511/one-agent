"""MCP Server — One-Agent as an MCP server, allowing other agents to call its tools.

Provides:
  - MCP Server implementation (JSON-RPC over stdio/HTTP)
  - Expose internal skills/tools to external MCP clients
  - Tool discovery, call, and resource listing
  - Compatible with Claude Desktop, Cursor, and other MCP clients
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MCPServer:
    """MCP (Model Context Protocol) server implementation.

    Allows other AI agents (Claude Desktop, Cursor, etc.) to discover
    and call One-Agent's skills as MCP tools.

    Supports:
      - stdio transport (for Claude Desktop)
      - HTTP transport (for remote access)
    """

    def __init__(self, name: str = "one-agent", version: str = "1.0.0"):
        self._name = name
        self._version = version
        self._skills: Dict[str, Any] = {}
        self._running = False
        self._transport = "stdio"  # stdio or http
        self._http_port = 18888

    def register_skill(self, name: str, skill: Any) -> None:
        """Register a skill to be exposed as an MCP tool."""
        self._skills[name] = skill

    def register_skills(self, skills: Dict[str, Any]) -> None:
        """Register multiple skills at once."""
        self._skills.update(skills)

    # --------------------------------------------------- MCP protocol

    def _build_tools_list(self) -> List[Dict[str, Any]]:
        """Build the tools/list response with all registered skills."""
        tools = []
        for name, skill in self._skills.items():
            try:
                schema = skill.get_skill_schema() if hasattr(skill, "get_skill_schema") else {}
                tools.append({
                    "name": name,
                    "description": schema.get("description", getattr(skill, "description", name)),
                    "inputSchema": schema.get("parameters", {
                        "type": "object",
                        "properties": {
                            "input": {
                                "type": "string",
                                "description": f"Input for {name}",
                            },
                        },
                    }),
                })
            except Exception as exc:
                logger.warning("mcp: failed to build tool schema for %s: %s", name, exc)
        return tools

    async def _call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call a registered tool."""
        skill = self._skills.get(name)
        if skill is None:
            return {
                "content": [{"type": "text", "text": f"Tool '{name}' not found"}],
                "isError": True,
            }

        try:
            if hasattr(skill, "run"):
                result = await skill.run(arguments)
            elif callable(skill):
                result = await skill(arguments)
            else:
                result = str(skill)

            return {
                "content": [{"type": "text", "text": str(result)}],
                "isError": False,
            }
        except Exception as exc:
            logger.error("mcp: tool call %s failed: %s", name, exc)
            return {
                "content": [{"type": "text", "text": f"Error: {exc}"}],
                "isError": True,
            }

    async def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle an MCP JSON-RPC request."""
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {})

        error_response = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

        try:
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {
                            "name": self._name,
                            "version": self._version,
                        },
                        "capabilities": {
                            "tools": {},
                        },
                    },
                }

            elif method == "notifications/initialized":
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}

            elif method == "tools/list":
                tools = self._build_tools_list()
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": tools},
                }

            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})
                result = await self._call_tool(tool_name, arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": result,
                }

            elif method == "ping":
                return {"jsonrpc": "2.0", "id": req_id, "result": {}}

            return error_response

        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(exc)},
            }

    # --------------------------------------------------- transports

    async def run_stdio(self) -> None:
        """Run MCP server over stdio (for Claude Desktop / Cursor)."""
        self._transport = "stdio"
        self._running = True
        logger.info("MCP server: listening on stdio")

        loop = asyncio.get_running_loop()

        while self._running:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("mcp: invalid JSON: %s", line[:100])
                    continue

                response = await self.handle_request(request)

                response_str = json.dumps(response, ensure_ascii=False)
                sys.stdout.write(response_str + "\n")
                sys.stdout.flush()

            except KeyboardInterrupt:
                break
            except Exception as exc:
                logger.error("mcp stdio error: %s", exc)
                break

        self._running = False

    async def run_http(self, host: str = "127.0.0.1", port: int = 18888) -> None:
        """Run MCP server over HTTP."""
        try:
            from fastapi import FastAPI, Request
            from fastapi.responses import JSONResponse
        except ImportError:
            logger.error("MCP HTTP server requires fastapi: pip install fastapi uvicorn")
            return

        self._transport = "http"
        self._http_port = port

        app = FastAPI(title="One-Agent MCP Server", version=self._version)
        self_ref = self

        @app.post("/mcp")
        async def mcp_endpoint(request: Request):
            body = await request.json()
            response = await self_ref.handle_request(body)
            return JSONResponse(content=response)

        @app.get("/mcp/health")
        async def health():
            return {"status": "ok", "tools": len(self_ref._skills)}

        logger.info("MCP server: listening on http://%s:%d/mcp", host, port)

        try:
            import uvicorn
            config = uvicorn.Config(app, host=host, port=port, log_level="warning")
            server = uvicorn.Server(config)
            self._running = True
            await server.serve()
        except Exception as exc:
            logger.error("MCP HTTP server failed: %s", exc)

    def stop(self) -> None:
        self._running = False


# Singleton
_mcp_server: Optional[MCPServer] = None


def get_mcp_server() -> MCPServer:
    global _mcp_server
    if _mcp_server is None:
        _mcp_server = MCPServer()
    return _mcp_server