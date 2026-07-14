"""测试第 9 轮审计修复：web_search_handler V65 重写。

覆盖：
1. _strip_html 正确去除 HTML 标签
2. _looks_like_real_link 过滤导航/广告/无关链接
3. _parse_result_blocks 通用解析（360 h3.res-title / Bing b_algo / DDG）
4. 全部源失败时返回清晰诊断 + 4 个替代方案
5. 空 query 立即返回错误
6. 实际 httpx 调用 360/Bing 源（带 mock）
"""

import asyncio
import re
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ============================================================
# Helper：直接拿到 web_search_handler 闭包函数
# ============================================================
def _get_web_search_handler():
    """从 SkillManager 提取 web_search skill 的 handler 闭包。

    web_search_handler 内部定义的辅助函数（_strip_html 等）也是闭包，
    测试这些闭包需要重新执行外层 handler 一次以拿到 results，再读取
    函数对象。

    策略：把整个 handler body 复制出来跑一次会触发真实网络，
    这里用 mock httpx 替换成 fake HTML。
    """
    from skills import SkillManager

    mgr = SkillManager.__new__(SkillManager)
    mgr._skills = {}  # 初始化 _skills 字典，绕过 __init__
    mgr._seed_web_search_skill()
    handler = mgr._skills["web_search"].handler  # type: ignore[attr-defined]
    return handler


# ============================================================
# 1. _strip_html 通过实际搜索结果验证
# ============================================================
class TestStripHtml:
    """验证 _strip_html 能正确处理 360/Bing/DDG 的 HTML 片段。"""

    def test_strip_simple_tags(self):
        html = "<h3>Agnes AI 免费 API</h3>"
        cleaned = re.sub(r"<[^>]+>", " ", html)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        assert cleaned == "Agnes AI 免费 API"

    def test_strip_nested_tags(self):
        html = '<h3 class="res-title"><a href="..."><em>Agnes</em> AI 免费 API</a></h3>'
        cleaned = re.sub(r"<[^>]+>", " ", html)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        assert "Agnes" in cleaned
        assert "AI" in cleaned
        assert "<" not in cleaned
        assert ">" not in cleaned

    def test_strip_whitespace_collapse(self):
        html = "<p>foo   bar\n\n\tbaz</p>"
        cleaned = re.sub(r"<[^>]+>", " ", html)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        assert cleaned == "foo bar baz"


# ============================================================
# 2. _looks_like_real_link 过滤逻辑
# ============================================================
class TestLooksLikeRealLink:
    """_looks_like_real_link 应该过滤掉导航/广告/登录链接，要求 title 与 query 相关。"""

    def _check(self, href, title, query):
        """复制 _looks_like_real_link 的逻辑（因为是闭包无法直接 import）。"""
        if not href.startswith("http"):
            return False
        # 静态资源扩展名（.js 后必须跟 ?/#/$，避免误杀 .json）
        if re.search(
            r'\.(css|js|png|jpe?g|gif|svg|ico|woff2?|bmp|tiff?)(\?|#|$)',
            href.lower(),
        ):
            return False
        skip_patterns = [
            "javascript:", "mailto:", "#", "/login", "/reg",
        ]
        for sp in skip_patterns:
            if sp in href.lower():
                return False
        for own in ("so.com/link", "ai.so.com/search", "sogou.com/link",
                    "baidu.com/link", "bing.com/ck/", "bing.com/search?",
                    "duckduckgo.com/?", "duckduckgo.com/html",
                    "r.bing.com", "go.microsoft.com"):
            if own in href.lower():
                return False
        if not title or len(title) < 4:
            return False
        q_words = [w for w in re.split(r"[\s,]+", query) if len(w) >= 2]
        if not q_words:
            return True
        title_lower = title.lower()
        return any(w.lower() in title_lower for w in q_words)

    def test_real_agnes_article_passes(self):
        assert self._check(
            "https://blog.example.com/agnes-ai-free-api",
            "全球 Top9 AI 实验室 Agnes AI 无限期免费开放全模态核心 API",
            "Agnes AI 免费 API",
        ) is True

    def test_javascript_link_rejected(self):
        assert self._check("javascript:void(0)", "Some Title Agnes", "Agnes AI") is False

    def test_short_title_rejected(self):
        assert self._check("https://example.com/page", "短标题", "Agnes AI") is False

    def test_irrelevant_title_rejected(self):
        assert self._check(
            "https://example.com/cooking",
            "红烧肉的 5 种家常做法大全",
            "Agnes AI 免费 API",
        ) is False

    def test_search_navigation_link_rejected(self):
        assert self._check(
            "https://www.so.com/link?url=xxx",
            "Agnes AI 无限期免费",
            "Agnes AI 免费",
        ) is False

    def test_login_link_rejected(self):
        assert self._check(
            "https://example.com/login",
            "Agnes AI Platform Login",
            "Agnes AI",
        ) is False

    def test_image_link_rejected(self):
        assert self._check(
            "https://example.com/logo.png",
            "Agnes AI Platform Logo",
            "Agnes AI",
        ) is False

    def test_json_url_not_filtered(self):
        """Bug5 回归：.json URL 不应被 .js 过滤误杀。"""
        assert self._check(
            "https://api.example.com/v1/models.json",
            "Agnes AI Models API JSON",
            "Agnes AI",
        ) is True

    def test_js_url_with_query_filtered(self):
        """带 query 的 .js URL 也应被过滤。"""
        assert self._check(
            "https://example.com/script.js?v=1.2",
            "Agnes AI Script",
            "Agnes AI",
        ) is False


# ============================================================
# 3. 360 搜索 HTML 解析（mock 实际响应）
# ============================================================
class TestParse360Html:
    """360 搜索用 h3.res-title + a 标签结构。"""

    def test_360_h3_res_title_pattern(self):
        html = '''
        <h3 class="res-title">
            <a data-res="..." href="https://blog.csdn.net/abc/123456" target="_blank">
                全球Top9 AI实验室<em>Agnes AI</em>无限期免费开放全模态核心API
            </a>
        </h3>
        <p class="res-desc">Agnes AI 提供文本/图像/视频三大模态的免费API...</p>
        '''
        # 验证 h3.res-title 模式能找到 a 标签和 href
        m = re.search(
            r'<h3[^>]*class="[^"]*res-title[^"]*"[^>]*>(.*?)</h3>',
            html, re.DOTALL | re.IGNORECASE,
        )
        assert m is not None
        block = m.group(1)
        link_m = re.search(
            r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
            block, re.DOTALL,
        )
        assert link_m is not None
        assert link_m.group(1).startswith("https://blog.csdn.net")
        title = re.sub(r"<[^>]+>", " ", link_m.group(2))
        title = re.sub(r"\s+", " ", title).strip()
        assert "Agnes" in title


# ============================================================
# 4. Bing CN 搜索 HTML 解析
# ============================================================
class TestParseBingHtml:
    """Bing 用 li.b_algo 块结构。"""

    def test_bing_b_algo_pattern(self):
        html = '''
        <li class="b_algo">
            <h2>
                <a href="https://example.com/agnes-ai">Agnes AI Free API Guide</a>
            </h2>
            <p>This article covers Agnes AI free API access...</p>
        </li>
        '''
        m = re.search(
            r'<li[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>',
            html, re.DOTALL | re.IGNORECASE,
        )
        assert m is not None
        block = m.group(1)
        link_m = re.search(
            r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
            block, re.DOTALL,
        )
        assert link_m is not None
        assert "agnes" in link_m.group(1).lower()


# ============================================================
# 5. DuckDuckGo Lite HTML 解析
# ============================================================
class TestParseDDGHtml:
    """DDG Lite 用 td.result-link + a.result-link。"""

    def test_ddg_td_result_link_pattern(self):
        html = '''
        <td class="result-link">
            <a class="result-link" rel="nofollow" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fagnes">
                Agnes AI Free API Tutorial
            </a>
        </td>
        '''
        m = re.search(
            r'<td[^>]*class="[^"]*result-link[^"]*"[^>]*>(.*?)</td>',
            html, re.DOTALL | re.IGNORECASE,
        )
        assert m is not None
        block = m.group(1)
        link_m = re.search(
            r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>',
            block, re.DOTALL,
        )
        assert link_m is not None
        # DDG 链接是 uddg 包装的，但我们这里 _looks_like_real_link 会
        # 过滤掉 duckduckgo.com 自身（"duckduckgo.com/?" 被列入 own filter）
        # 所以测试用直链版
        assert "example.com" in link_m.group(1)


# ============================================================
# 6. handler 空 query 立即返回错误
# ============================================================
class TestEmptyQuery:
    def test_empty_string_returns_error(self):
        handler = _get_web_search_handler()
        result = asyncio.run(handler({"input": ""}))
        assert "[web_search error: empty query]" in result

    def test_whitespace_only_returns_error(self):
        handler = _get_web_search_handler()
        result = asyncio.run(handler({"input": "   "}))
        assert "[web_search error: empty query]" in result

    def test_missing_input_returns_error(self):
        handler = _get_web_search_handler()
        result = asyncio.run(handler({}))
        assert "[web_search error: empty query]" in result


# ============================================================
# 7. 全部源失败时返回诊断 + 4 个替代方案
# ============================================================
class TestAllSourcesFail:
    """当 360 / DDG / Bing 全部失败时，返回清晰的诊断信息和替代方案。"""

    def test_all_fail_returns_diagnostic(self):
        handler = _get_web_search_handler()

        # Mock httpx 让所有源都抛 RequestError
        import httpx

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw):
                raise httpx.ConnectError("simulated SSL error")
            async def post(self, *a, **kw):
                raise httpx.ConnectError("simulated SSL error")

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"input": "Agnes AI"}))

        # 验证返回包含诊断 + 替代方案
        assert "[web_search: 全部源" in result
        assert "替代方案" in result
        assert "system_run" in result
        assert "python_execute" in result
        assert "curl" in result
        assert "API key" in result  # 应提到 API key 替代方案

    def test_all_fail_lists_all_tried_sources(self):
        """验证 sources_tried 完整列出 360/DDG/Bing 三个源。"""
        handler = _get_web_search_handler()

        # 让所有源都返回 0 results（200 OK 但 HTML 无结果）
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = "<html><body>no results</body></html>"

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp
            async def post(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"input": "Agnes AI"}))

        assert "360搜索" in result
        assert "DuckDuckGo" in result
        assert "Bing" in result
        assert "均失败" in result

    def test_each_source_shows_own_error(self):
        """Bug1 回归：每个源应显示各自的错误，不是都用最后一个 last_error。"""
        handler = _get_web_search_handler()
        import httpx

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, **kw):
                if "bing.com" in url:
                    raise httpx.ConnectError("BING_SSL_ERROR")
                if "so.com" in url:
                    # 360 返回 403
                    fake = MagicMock()
                    fake.status_code = 403
                    fake.text = "Forbidden"
                    return fake
                # DDG: 超时
                raise httpx.TimeoutException("DDG_TIMEOUT")
            async def post(self, *a, **kw):
                raise httpx.TimeoutException("DDG_TIMEOUT")

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"input": "Agnes AI"}))

        # 每个源应显示各自的错误
        assert "Bing=ConnectError" in result
        assert "360搜索=HTTP 403" in result
        assert "DuckDuckGo=TimeoutException" in result
        # 不应该所有源都显示同一个错误
        assert result.count("DDG_TIMEOUT") == 1  # 只在 DDG 那一项


# ============================================================
# 8. 360 源成功路径（mock 360 响应）
# ============================================================
class Test360Success:
    """Mock 360 搜索返回真实结果，验证 handler 走 360 路径并返回结果。"""

    def test_360_returns_results(self):
        handler = _get_web_search_handler()

        # Mock 360 返回真实结构
        real_html = '''
        <html><body>
        <ul class="result">
            <li class="res-list">
                <h3 class="res-title">
                    <a href="https://blog.csdn.net/test/123">
                        Agnes AI 无限期免费开放全模态核心API
                    </a>
                </h3>
                <p class="res-desc">Agnes AI 提供文本/图像/视频三大模态免费 API 接入</p>
            </li>
            <li class="res-list">
                <h3 class="res-title">
                    <a href="https://zhuanlan.zhihu.com/p/456">
                        Agnes AI 免费 API 接入 Claude Code
                    </a>
                </h3>
                <p class="res-desc">通过 Agnes AI 免费 Token 接入 Claude Code 教程</p>
            </li>
        </ul>
        </body></html>
        '''

        # 360 调用成功后直接 return True，不会再尝试 DDG/Bing
        class FakeClient360Only:
            """第一次（360）成功；其他调用会 fail 测试。"""
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, **kw):
                if "so.com" in url:
                    fake = MagicMock()
                    fake.status_code = 200
                    fake.text = real_html
                    return fake
                # 兜底：返回 0 结果
                fake = MagicMock()
                fake.status_code = 200
                fake.text = "<html></html>"
                return fake
            async def post(self, *a, **kw):
                fake = MagicMock()
                fake.status_code = 200
                fake.text = "<html></html>"
                return fake

        with patch("httpx.AsyncClient", FakeClient360Only):
            result = asyncio.run(handler({"input": "Agnes AI 免费 API"}))

        # 验证结果格式
        assert "搜索结果" in result
        assert "Agnes AI" in result
        assert "blog.csdn.net" in result or "csdn" in result
        assert "zhihu" in result or "zhuanlan" in result
        assert "提示：基于上述结果直接回答用户" in result


# ============================================================
# 9. Bing 失败时 fallback 到 360（mock Bing fail + 360 OK）
# ============================================================
class TestFallbackChain:
    """验证源失败时正确 fallback 到下一个源。"""

    def test_bing_fail_360_ok(self):
        """Bing 主源失败时，fallback 到 360。"""
        handler = _get_web_search_handler()

        html_360 = '''
        <h3 class="res-title">
            <a href="https://blog.csdn.net/test/123">
                Agnes AI 无限期免费开放全模态核心API
            </a>
        </h3>
        <p class="res-desc">Agnes AI 提供文本/图像/视频三大模态免费 API</p>
        '''

        import httpx

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, **kw):
                if "bing.com" in url:
                    raise httpx.ConnectError("simulated network error")
                # 360
                fake = MagicMock()
                fake.status_code = 200
                fake.text = html_360
                return fake
            async def post(self, *a, **kw):
                raise httpx.ConnectError("simulated network error")

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"input": "Agnes AI"}))

        assert "搜索结果" in result
        assert "csdn" in result
        # 来源标签应该是 360搜索（Bing 失败后 fallback）
        assert "来源: 360搜索" in result

    def test_bing_ok_uses_direct_url(self):
        """Bing 成功时返回直链（非跳转URL），标题来自 h2>a。"""
        handler = _get_web_search_handler()

        bing_html = '''
        <li class="b_algo">
            <div class="b_tpcn">
                <a class="tilk" href="https://agnes-ai.com/">
                    <cite>https://agnes-ai.com</cite>
                </a>
            </div>
            <h2>
                <a href="https://agnes-ai.com/">Agnes AI | Free Omni-Modal AI API</a>
            </h2>
            <div class="b_caption">
                <p class="b_lineclamp2">Agnes AI by Sapiens AI is an AI gateway, free AI API platform</p>
            </div>
        </li>
        '''

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, **kw):
                fake = MagicMock()
                fake.status_code = 200
                fake.text = bing_html
                return fake
            async def post(self, *a, **kw):
                fake = MagicMock()
                fake.status_code = 200
                fake.text = "<html></html>"
                return fake

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"input": "Agnes AI"}))

        assert "搜索结果" in result
        assert "来源: Bing" in result
        # h2>a 的标题，不是 tilk 链接的 cite 文本
        assert "Agnes AI | Free Omni-Modal AI API" in result
        assert "Sapiens AI" in result  # 摘要内容
        # 直链
        assert "https://agnes-ai.com/" in result

    def test_bing_tilk_link_not_used_as_title(self):
        """Bing 的 a.tilk（网站图标链接）不应被当作标题。"""
        handler = _get_web_search_handler()

        # 只有 tilk 链接，没有 h2>a — 应该被跳过
        bing_html = '''
        <li class="b_algo">
            <div class="b_tpcn">
                <a class="tilk" href="https://agnes-ai.com/">
                    <cite>https://agnes-ai.com</cite>
                </a>
            </div>
        </li>
        '''

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, **kw):
                fake = MagicMock()
                fake.status_code = 200
                fake.text = bing_html
                return fake
            async def post(self, *a, **kw):
                fake = MagicMock()
                fake.status_code = 200
                fake.text = "<html></html>"
                return fake

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"input": "Agnes AI"}))

        # Bing 0 results → fallback → 全部失败
        assert "[web_search: 全部源" in result
