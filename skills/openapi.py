"""OpenAPI Auto-Parser — parse OpenAPI/Swagger specs and auto-generate API tools.

Provides:
  - Parse OpenAPI 3.x / Swagger 2.0 specs (JSON/YAML)
  - Auto-generate tool schemas from API endpoints
  - Call any API endpoint with proper auth
  - Cache parsed schemas
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

logger = logging.getLogger(__name__)


class OpenAPISkill:
    """Auto-generate API integration tools from OpenAPI specs.

    Given an OpenAPI spec URL or file, this skill can:
    1. Parse the spec and list all endpoints
    2. Auto-generate tool schemas for each endpoint
    3. Call any endpoint with proper parameter handling
    """

    name = "openapi"
    description = "Auto-parse OpenAPI specs and generate API tools"

    def __init__(self):
        self._specs: Dict[str, Dict[str, Any]] = {}
        self._base_urls: Dict[str, str] = {}
        self._auth: Dict[str, Dict[str, str]] = {}

    def load_spec(
        self, name: str, spec: Dict[str, Any], base_url: str = "",
        auth: Optional[Dict[str, str]] = None,
    ) -> None:
        """Load an OpenAPI spec.

        Args:
            name: name for this API
            spec: parsed OpenAPI spec dict
            base_url: base URL for API calls
            auth: auth config {"type": "bearer"/"api_key"/"basic", "token": "..."}
        """
        self._specs[name] = spec
        self._base_urls[name] = base_url or self._extract_base_url(spec)
        self._auth[name] = auth or {}

    def _extract_base_url(self, spec: Dict[str, Any]) -> str:
        """Extract base URL from OpenAPI spec."""
        if "servers" in spec and spec["servers"]:
            return spec["servers"][0].get("url", "")
        if "host" in spec:
            scheme = spec.get("schemes", ["https"])[0]
            base_path = spec.get("basePath", "")
            return f"{scheme}://{spec['host']}{base_path}"
        return ""

    # --------------------------------------------------- list endpoints

    def list_endpoints(self, name: str) -> List[Dict[str, Any]]:
        """List all endpoints in a spec."""
        spec = self._specs.get(name)
        if not spec:
            return []

        endpoints = []
        paths = spec.get("paths", {})

        for path, methods in paths.items():
            for method, details in methods.items():
                if method.upper() not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                    continue
                endpoints.append({
                    "method": method.upper(),
                    "path": path,
                    "summary": details.get("summary", ""),
                    "description": details.get("description", ""),
                    "operation_id": details.get("operationId", ""),
                    "tags": details.get("tags", []),
                })

        return endpoints

    def search_endpoints(
        self, name: str, keyword: str,
    ) -> List[Dict[str, Any]]:
        """Search endpoints by keyword."""
        keyword_lower = keyword.lower()
        endpoints = self.list_endpoints(name)
        return [
            e for e in endpoints
            if keyword_lower in e["path"].lower()
            or keyword_lower in e.get("summary", "").lower()
            or keyword_lower in e.get("description", "").lower()
            or keyword_lower in str(e.get("tags", "")).lower()
        ]

    # --------------------------------------------------- call API

    async def call(
        self,
        name: str,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Call an API endpoint.

        Args:
            name: API spec name
            method: HTTP method (GET, POST, PUT, DELETE)
            path: API path
            params: query parameters
            body: request body (for POST/PUT)
            headers: additional headers
        """
        spec = self._specs.get(name)
        base_url = self._base_urls.get(name, "")
        auth = self._auth.get(name, {})

        if not base_url:
            return {"ok": False, "error": f"API '{name}' 未配置 base_url"}

        url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))

        import httpx

        # Build headers with auth
        req_headers = headers or {}
        auth_type = auth.get("type", "")
        if auth_type == "bearer":
            req_headers["Authorization"] = f"Bearer {auth.get('token', '')}"
        elif auth_type == "api_key":
            key_name = auth.get("key_name", "X-API-Key")
            key_location = auth.get("key_location", "header")
            if key_location == "header":
                req_headers[key_name] = auth.get("token", "")

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                if method.upper() == "GET":
                    resp = await client.get(url, params=params, headers=req_headers)
                elif method.upper() == "POST":
                    resp = await client.post(url, params=params, json=body, headers=req_headers)
                elif method.upper() == "PUT":
                    resp = await client.put(url, params=params, json=body, headers=req_headers)
                elif method.upper() == "DELETE":
                    resp = await client.delete(url, params=params, headers=req_headers)
                elif method.upper() == "PATCH":
                    resp = await client.patch(url, params=params, json=body, headers=req_headers)
                else:
                    return {"ok": False, "error": f"不支持的 HTTP 方法: {method}"}

                try:
                    data = resp.json()
                except Exception:
                    data = resp.text

                return {
                    "ok": resp.status_code < 400,
                    "status_code": resp.status_code,
                    "data": data,
                }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # --------------------------------------------------- parse spec from URL

    async def load_from_url(self, name: str, url: str, auth_token: str = "") -> Dict[str, Any]:
        """Load an OpenAPI spec from a URL."""
        import httpx

        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    return {"ok": False, "error": f"HTTP {resp.status_code}"}

                content_type = resp.headers.get("content-type", "")
                if "yaml" in content_type or url.endswith((".yaml", ".yml")):
                    try:
                        import yaml
                        spec = yaml.safe_load(resp.text)
                    except ImportError:
                        return {"ok": False, "error": "yaml 未安装。pip install pyyaml"}
                else:
                    spec = resp.json()

                self.load_spec(name, spec, auth={"type": "bearer", "token": auth_token})
                endpoints = self.list_endpoints(name)
                return {
                    "ok": True,
                    "name": name,
                    "title": spec.get("info", {}).get("title", name),
                    "version": spec.get("info", {}).get("version", ""),
                    "endpoints_count": len(endpoints),
                    "endpoints": endpoints[:20],
                }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # --------------------------------------------------- skill interface

    def get_skill_schema(self) -> Dict[str, Any]:
        return {
            "name": "openapi",
            "description": "Auto-parse OpenAPI specs and call API endpoints",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["load", "list", "search", "call"],
                        "description": "action: load spec, list endpoints, search, or call API",
                    },
                    "name": {"type": "string", "description": "API name"},
                    "url": {"type": "string", "description": "OpenAPI spec URL"},
                    "method": {"type": "string", "description": "HTTP method"},
                    "path": {"type": "string", "description": "API path"},
                    "keyword": {"type": "string", "description": "search keyword"},
                    "params": {"type": "object", "description": "query parameters"},
                    "body": {"type": "object", "description": "request body"},
                },
                "required": ["action"],
            },
        }

    async def run(self, args: Dict[str, Any]) -> str:
        """Execute openapi skill."""
        action = args.get("action", "list")

        if action == "load":
            result = await self.load_from_url(
                name=args.get("name", "default"),
                url=args.get("url", ""),
            )
            if result.get("ok"):
                return (
                    f"已加载 API: {result['title']} v{result['version']}\n"
                    f"共 {result['endpoints_count']} 个端点\n\n"
                    + "\n".join(
                        f"  {e['method']} {e['path']} — {e.get('summary', '')}"
                        for e in result.get("endpoints", [])[:20]
                    )
                )
            return f"加载失败: {result.get('error')}"

        elif action == "list":
            endpoints = self.list_endpoints(args.get("name", "default"))
            if not endpoints:
                return "未找到端点。请先 /openapi load <url>"
            return "\n".join(
                f"  {e['method']:6} {e['path']:40} {e.get('summary', '')}"
                for e in endpoints[:50]
            )

        elif action == "search":
            endpoints = self.search_endpoints(
                args.get("name", "default"),
                args.get("keyword", ""),
            )
            if not endpoints:
                return f"未找到匹配 '{args.get('keyword', '')}' 的端点"
            return "\n".join(
                f"  {e['method']} {e['path']} — {e.get('summary', '')}"
                for e in endpoints[:20]
            )

        elif action == "call":
            result = await self.call(
                name=args.get("name", "default"),
                method=args.get("method", "GET"),
                path=args.get("path", ""),
                params=args.get("params"),
                body=args.get("body"),
            )
            if result.get("ok"):
                data = result.get("data", {})
                if isinstance(data, dict):
                    return json.dumps(data, ensure_ascii=False, indent=2)[:2000]
                return str(data)[:2000]
            return f"API 调用失败: {result.get('error')}"

        return "未知操作"


# Singleton
_openapi_skill: Optional[OpenAPISkill] = None


def get_openapi_skill() -> OpenAPISkill:
    global _openapi_skill
    if _openapi_skill is None:
        _openapi_skill = OpenAPISkill()
    return _openapi_skill