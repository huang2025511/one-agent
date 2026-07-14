"""测试 web_fetch 技能（V65.2 新增）。

覆盖：
1. 空 URL / 无效 URL 返回错误
2. JSON API 响应直接返回格式化 JSON
3. HTML 正文提取（去标签、去噪声、保留结构）
4. 网络错误时返回清晰诊断
5. JS 渲染页面检测（正文过短提示）
"""

import asyncio
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _get_web_fetch_handler():
    """从 SkillManager 提取 web_fetch skill 的 handler 闭包。"""
    from skills import SkillManager

    mgr = SkillManager.__new__(SkillManager)
    mgr._skills = {}
    mgr._seed_web_search_skill()
    handler = mgr._skills["web_fetch"].handler  # type: ignore[attr-defined]
    return handler


# ============================================================
# 1. 参数校验
# ============================================================
class TestUrlValidation:
    def test_empty_url_returns_error(self):
        handler = _get_web_fetch_handler()
        result = asyncio.run(handler({"url": ""}))
        assert "[web_fetch error: empty url]" in result

    def test_non_http_url_returns_error(self):
        handler = _get_web_fetch_handler()
        result = asyncio.run(handler({"url": "ftp://example.com"}))
        assert "must start with http" in result

    def test_plain_text_url_returns_error(self):
        handler = _get_web_fetch_handler()
        result = asyncio.run(handler({"url": "example.com"}))
        assert "must start with http" in result


# ============================================================
# 2. JSON API 响应
# ============================================================
class TestJsonResponse:
    def test_json_content_type_returns_formatted_json(self):
        handler = _get_web_fetch_handler()

        json_body = '{"models": [{"id": "agnes-2.0-flash"}, {"id": "agnes-image-2.1"}]}'

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = json_body
        fake_resp.content = json_body.encode()
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {"content-type": "application/json"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://api.example.com/v1/models"}))

        assert "agnes-2.0-flash" in result
        assert "agnes-image-2.1" in result
        # 应该是格式化的 JSON
        assert '"models"' in result


# ============================================================
# 3. HTML 正文提取
# ============================================================
class TestHtmlExtraction:
    def test_removes_script_and_style(self):
        handler = _get_web_fetch_handler()

        html = (
            "<html><head><script>alert('xss')</script>"
            "<style>body { color: red; }</style></head>"
            "<body><article>"
            "<p>这是正文内容，包含足够字符以确保不会被过滤掉。</p>"
            "<p>第二段正文，补充更多内容来确保总长度超过最小检查阈值。</p>"
            "<p>第三段正文，Agnes AI 免费 API 接入指南的核心内容。</p>"
            "</article></body></html>"
        )

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = html
        fake_resp.content = html.encode()
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {"content-type": "text/html"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/page"}))

        assert "alert" not in result
        assert "color: red" not in result
        assert "这是正文内容" in result

    def test_removes_nav_footer(self):
        handler = _get_web_fetch_handler()

        html = (
            "<html><body>"
            "<nav><a href='/'>首页</a> <a href='/about'>关于</a></nav>"
            "<article>"
            "<p>正文段落一，足够长的内容确保通过最小长度检查机制不会被误判为JS渲染页面。</p>"
            "<p>正文段落二，更多详细内容来补充正文的长度和可读性。</p>"
            "<p>正文段落三，Agnes AI API 接入的具体步骤和配置说明。</p>"
            "</article>"
            "<footer>Copyright 2025. All rights reserved.</footer>"
            "</body></html>"
        )

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = html
        fake_resp.content = html.encode()
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {"content-type": "text/html"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/page"}))

        assert "Copyright" not in result
        assert "正文段落一" in result

    def test_html_entity_decode(self):
        handler = _get_web_fetch_handler()

        html = (
            "<html><body><article>"
            "<p>Agnes AI &amp; Sapiens AI &#0183; Free API &ensp; Test</p>"
            "<p>补充段落确保内容长度足够通过最小检查阈值，不会被误判为短页面。</p>"
            "</article></body></html>"
        )

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = html
        fake_resp.content = html.encode()
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {"content-type": "text/html"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/page"}))

        assert "&amp;" not in result
        assert "Agnes AI & Sapiens AI" in result

    def test_max_chars_truncation(self):
        handler = _get_web_fetch_handler()

        long_text = "A" * 10000
        html = "<html><body><article><p>" + long_text + "</p></article></body></html>"

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = html
        fake_resp.content = html.encode()
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {"content-type": "text/html"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/page", "max_chars": 500}))

        assert "已截断" in result
        assert len(result) < 2000  # 截断后总长度应该可控


# ============================================================
# 4. 网络错误处理
# ============================================================
class TestNetworkError:
    def test_connection_error_returns_diagnostic(self):
        handler = _get_web_fetch_handler()
        import httpx

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw):
                raise httpx.ConnectError("SSL error")

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com"}))

        assert "[web_fetch error:" in result
        assert "ConnectError" in result or "SSL" in result

    def test_http_error_status(self):
        handler = _get_web_fetch_handler()

        fake_resp = MagicMock()
        fake_resp.status_code = 403
        fake_resp.text = "Forbidden"
        fake_resp.headers = {"content-type": "text/html"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com"}))

        assert "[web_fetch error: HTTP 403]" in result


# ============================================================
# 4b. max_chars 无效值处理（Bug3 回归）
# ============================================================
class TestMaxCharsValidation:
    def test_non_integer_max_chars_uses_default(self):
        handler = _get_web_fetch_handler()

        html = (
            "<html><body><article>"
            "<h1>Agnes AI 免费 API 接入完整指南</h1>"
            "<p>正文第一段：Agnes AI 是由 Sapiens AI 开发的全模态 AI 平台，"
            "提供文本生成、图像生成和视频生成三大核心 API 服务。</p>"
            "<p>正文第二段：该平台长期免费开放，用户只需注册即可获取 API Key，"
            "支持 OpenAI 兼容格式，可直接接入 Claude Code 等工具。</p>"
            "<p>正文第三段：接入方式简单，Base URL 为 apihub.agnes-ai.com/v1，"
            "与 OpenAI SDK 完全兼容。</p>"
            "</article></body></html>"
        )
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = html
        fake_resp.content = html.encode()
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {"content-type": "text/html"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            # LLM 传入字符串 "abc" 而非整数
            result = asyncio.run(handler({"url": "https://example.com/page", "max_chars": "abc"}))

        assert "Agnes AI" in result  # 正常返回，不崩溃

    def test_negative_max_chars_uses_default(self):
        handler = _get_web_fetch_handler()

        html = (
            "<html><body><article>"
            "<h1>Agnes AI 免费 API 接入完整指南</h1>"
            "<p>正文第一段：Agnes AI 是由 Sapiens AI 开发的全模态 AI 平台，"
            "提供文本生成、图像生成和视频生成三大核心 API 服务。</p>"
            "<p>正文第二段：该平台长期免费开放，用户只需注册即可获取 API Key，"
            "支持 OpenAI 兼容格式，可直接接入 Claude Code 等工具。</p>"
            "</article></body></html>"
        )
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = html
        fake_resp.content = html.encode()
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {"content-type": "text/html"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/page", "max_chars": -1}))

        assert "Agnes AI" in result

    def test_zero_max_chars_uses_default(self):
        handler = _get_web_fetch_handler()

        html = (
            "<html><body><article>"
            "<h1>Agnes AI 免费 API 接入完整指南</h1>"
            "<p>正文第一段：Agnes AI 是由 Sapiens AI 开发的全模态 AI 平台，"
            "提供文本生成、图像生成和视频生成三大核心 API 服务。</p>"
            "<p>正文第二段：该平台长期免费开放，用户只需注册即可获取 API Key，"
            "支持 OpenAI 兼容格式，可直接接入 Claude Code 等工具。</p>"
            "</article></body></html>"
        )
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = html
        fake_resp.content = html.encode()
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {"content-type": "text/html"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/page", "max_chars": 0}))

        assert "Agnes AI" in result


# ============================================================
# 4c. 二进制内容检测（Bug4 回归）
# ============================================================
class TestBinaryContent:
    def test_pdf_returns_binary_warning(self):
        handler = _get_web_fetch_handler()

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = "%PDF-1.4 binary..."
        fake_resp.headers = {"content-type": "application/pdf"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/doc.pdf"}))

        assert "二进制内容" in result
        assert "application/pdf" in result

    def test_image_returns_binary_warning(self):
        handler = _get_web_fetch_handler()

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = "binary image data..."
        fake_resp.headers = {"content-type": "image/png"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/photo.png"}))

        assert "二进制内容" in result
        assert "image/png" in result

    def test_zip_returns_binary_warning(self):
        handler = _get_web_fetch_handler()

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = "PK\x03\x04 binary..."
        fake_resp.headers = {"content-type": "application/zip"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/file.zip"}))

        assert "二进制内容" in result


# ============================================================
# 4d. SSRF 防护（P0 安全修复）
# ============================================================
class TestSSRFProtection:
    """web_fetch 必须拒绝内网/回环/链路本地地址。"""

    def test_localhost_rejected(self):
        handler = _get_web_fetch_handler()
        result = asyncio.run(handler({"url": "http://localhost:8080/admin"}))
        assert "访问被拒绝" in result

    def test_127_loopback_rejected(self):
        handler = _get_web_fetch_handler()
        result = asyncio.run(handler({"url": "http://127.0.0.1:8080/admin"}))
        assert "访问被拒绝" in result

    def test_127_subdomain_rejected(self):
        handler = _get_web_fetch_handler()
        result = asyncio.run(handler({"url": "http://127.0.0.1:6379/"}))
        assert "访问被拒绝" in result

    def test_192_168_private_rejected(self):
        handler = _get_web_fetch_handler()
        result = asyncio.run(handler({"url": "http://192.168.1.1/"}))
        assert "访问被拒绝" in result

    def test_10_private_rejected(self):
        handler = _get_web_fetch_handler()
        result = asyncio.run(handler({"url": "http://10.0.0.1/"}))
        assert "访问被拒绝" in result

    def test_169_254_metadata_rejected(self):
        """AWS/阿里云元数据服务 — 最危险的 SSRF 目标。"""
        handler = _get_web_fetch_handler()
        result = asyncio.run(handler({
            "url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
        }))
        assert "访问被拒绝" in result

    def test_172_16_private_rejected(self):
        handler = _get_web_fetch_handler()
        result = asyncio.run(handler({"url": "http://172.16.0.1/"}))
        assert "访问被拒绝" in result

    def test_172_32_public_allowed(self):
        """172.32.x.x 是公网地址，不应被拒绝。"""
        handler = _get_web_fetch_handler()

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = "<html><body><article><p>公网内容，确保足够长度通过检查。</p></article></body></html>"
        fake_resp.content = fake_resp.text.encode()
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {"content-type": "text/html"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        # 172.32.x.x 不在 172.16-31 私网范围内，不应被拒绝
        # 但 DNS 解析 172.32.0.1 可能不响应，这里 mock socket
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.side_effect = OSError("no DNS")
            with patch("httpx.AsyncClient", FakeClient):
                result = asyncio.run(handler({"url": "http://172.32.0.1/"}))

        assert "访问被拒绝" not in result

    def test_public_domain_allowed(self):
        """公网域名不应被拒绝。"""
        handler = _get_web_fetch_handler()

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = "<html><body><article><p>公网内容，确保足够长度通过检查。</p></article></body></html>"
        fake_resp.content = fake_resp.text.encode()
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {"content-type": "text/html"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://www.example.com/page"}))

        assert "访问被拒绝" not in result


# ============================================================
# 5. JS 渲染页面检测
# ============================================================
class TestJsRenderedPage:
    def test_short_content_warns_js_rendered(self):
        handler = _get_web_fetch_handler()

        # SPA 页面：body 几乎为空
        html = (
            "<html><head><script src='app.js'></script></head>"
            "<body><div id='root'></div></body></html>"
        )

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = html
        fake_resp.content = html.encode()
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {"content-type": "text/html"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/spa"}))

        assert "页面内容过短" in result
        assert "JS 渲染" in result
        assert "system_run" in result  # 建议用 curl


# ============================================================
# 6. article 正文优先提取
# ============================================================
class TestArticleExtraction:
    def test_article_tag_prioritized(self):
        handler = _get_web_fetch_handler()

        html = """
        <html><body>
        <div class="sidebar">侧边栏广告和导航链接</div>
        <article>
            <h1>Agnes AI 免费API接入指南</h1>
            <p>这是正文第一段，包含足够的内容来通过最小长度检查机制。</p>
            <p>这是正文第二段，详细介绍 API 接入步骤。</p>
            <p>这是正文第三段，包含更多技术细节和示例代码说明。</p>
        </article>
        <div class="comments">评论区内容</div>
        </body></html>
        """

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = html
        fake_resp.content = html.encode()
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {"content-type": "text/html"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/article"}))

        assert "Agnes AI 免费API接入指南" in result
        assert "正文第一段" in result
        assert "正文第二段" in result
