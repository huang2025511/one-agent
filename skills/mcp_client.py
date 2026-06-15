"""MCP (Model Context Protocol) Client — 连接外部 MCP 服务器。

MCP 是 Anthropic 发布的开放协议，允许 Agent 连接外部工具服务器：
- 数据库查询
- GitHub 操作
- 文件系统访问
- 自定义工具服务

架构：
- MCPClient: 管理多个 MCP 服务器连接
- MCPServer: 单个服务器连接和工具调用
- 自动发现服务器提供的工具
- 将 MCP 工具转换为 One-Agent skills
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


class MCPServer:
    """单个 MCP 服务器连接。"""
    
    def __init__(self, name: str, url: str, api_key: Optional[str] = None):
        self.name = name
        self.url = url
        self.api_key = api_key
        self.tools: List[Dict[str, Any]] = []
        self._client: Optional[httpx.AsyncClient] = None
        
    async def connect(self) -> bool:
        """连接到 MCP 服务器并发现工具。"""
        try:
            self._client = httpx.AsyncClient(timeout=30.0)
            
            # 获取服务器信息
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            
            # 发现工具
            response = await self._client.get(
                f"{self.url}/tools",
                headers=headers
            )
            response.raise_for_status()
            
            data = response.json()
            self.tools = data.get("tools", [])
            
            logger.info(f"MCP server '{self.name}' connected: {len(self.tools)} tools")
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect to MCP server '{self.name}': {e}")
            return False
    
    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """调用 MCP 工具。"""
        if not self._client:
            raise RuntimeError(f"MCP server '{self.name}' not connected")
        
        try:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            
            response = await self._client.post(
                f"{self.url}/tools/{tool_name}/call",
                headers=headers,
                json=arguments
            )
            response.raise_for_status()
            
            result = response.json()
            return result.get("result")
            
        except Exception as e:
            logger.error(f"MCP tool call failed: {tool_name} - {e}")
            raise
    
    async def close(self):
        """关闭连接。"""
        if self._client:
            await self._client.aclose()
            self._client = None


class MCPClient:
    """MCP 客户端管理器 — 管理多个 MCP 服务器。"""
    
    def __init__(self):
        self.servers: Dict[str, MCPServer] = {}
        
    async def add_server(self, name: str, url: str, api_key: Optional[str] = None) -> bool:
        """添加并连接 MCP 服务器。"""
        if name in self.servers:
            logger.warning(f"MCP server '{name}' already exists")
            return False
        
        server = MCPServer(name, url, api_key)
        success = await server.connect()
        
        if success:
            self.servers[name] = server
            return True
        
        return False
    
    async def remove_server(self, name: str):
        """移除 MCP 服务器。"""
        if name in self.servers:
            await self.servers[name].close()
            del self.servers[name]
    
    def list_tools(self) -> List[Dict[str, Any]]:
        """列出所有可用工具。"""
        all_tools = []
        for server_name, server in self.servers.items():
            for tool in server.tools:
                tool_copy = tool.copy()
                tool_copy["server"] = server_name
                all_tools.append(tool_copy)
        return all_tools
    
    async def call_tool(self, server_name: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """调用指定服务器的工具。"""
        if server_name not in self.servers:
            raise ValueError(f"MCP server '{server_name}' not found")
        
        return await self.servers[server_name].call_tool(tool_name, arguments)
    
    async def close_all(self):
        """关闭所有服务器连接。"""
        for server in self.servers.values():
            await server.close()
        self.servers.clear()


def mcp_tool_to_skill_schema(server: MCPServer, tool: Dict[str, Any]) -> Dict[str, Any]:
    """将 MCP 工具转换为 One-Agent skill schema。"""
    return {
        "name": f"mcp_{server.name}_{tool['name']}",
        "description": tool.get("description", f"MCP tool from {server.name}"),
        "parameters": tool.get("inputSchema", {}),
        "metadata": {
            "source": "mcp",
            "server": server.name,
            "tool_name": tool["name"]
        }
    }
