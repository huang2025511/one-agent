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
import ipaddress
import json
import logging
import socket
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# MCP client configuration
MCP_CONNECTION_TIMEOUT = 30.0
MCP_REQUEST_TIMEOUT = 30.0


def _is_private_ip(ip_str: str) -> bool:
    """Check if IP address is private/internal.
    
    Args:
        ip_str: IP address string (IPv4 or IPv6)
        
    Returns:
        True if IP is private/internal, False otherwise
    """
    try:
        ip = ipaddress.ip_address(ip_str)
        # Check if IP is private, loopback, link-local, or reserved
        return (
            ip.is_private or
            ip.is_loopback or
            ip.is_link_local or
            ip.is_reserved or
            ip.is_multicast
        )
    except ValueError:
        return True  # If we can't parse it, treat as private for safety


class MCPServer:
    """单个 MCP 服务器连接。"""
    
    def __init__(self, name: str, url: str, api_key: Optional[str] = None):
        # Security: Validate URL to prevent SSRF attacks
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            raise ValueError(f"Invalid URL scheme: {parsed.scheme}. Only http/https allowed.")
        
        # Block private/internal IPs to prevent SSRF
        hostname = parsed.hostname
        if hostname:
            # Try to resolve hostname to IP
            try:
                # Use getaddrinfo to handle both IPv4 and IPv6
                addr_infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
                for addr_info in addr_infos:
                    ip = addr_info[4][0]
                    if _is_private_ip(ip):
                        raise ValueError(f"Private/internal IP not allowed: {ip} (resolved from {hostname})")
            except socket.gaierror:
                # DNS resolution failed - will fail on connect anyway
                pass
        
        self.name = name
        self.url = url
        self.api_key = api_key
        self.tools: List[Dict[str, Any]] = []
        self._client: Optional[httpx.AsyncClient] = None
        
    async def connect(self) -> bool:
        """连接到 MCP 服务器并发现工具。"""
        try:
            self._client = httpx.AsyncClient(timeout=MCP_REQUEST_TIMEOUT)
            
            # 获取服务器信息
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            
            # 发现工具 — wrap with asyncio.wait_for for timeout safety
            try:
                response = await asyncio.wait_for(
                    self._client.get(
                        f"{self.url}/tools",
                        headers=headers
                    ),
                    timeout=MCP_CONNECTION_TIMEOUT
                )
            except asyncio.TimeoutError:
                raise TimeoutError(f"Connection to MCP server '{self.name}' timed out after {MCP_CONNECTION_TIMEOUT:.0f}s")
            response.raise_for_status()
            
            data = response.json()
            self.tools = data.get("tools", [])
            
            logger.info("MCP server '%s' connected: %d tools", self.name, len(self.tools))
            return True
            
        except TimeoutError:
            raise
        except Exception as e:
            logger.error("Failed to connect to MCP server '%s': %s", self.name, e)
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
            logger.error("MCP tool call failed: %s - %s", tool_name, e)
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
            logger.warning("MCP server '%s' already exists", name)
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
