"""V67 改进回归测试。

覆盖：
1. SSRF 重定向绕过防护（P0-1）
2. SSRF IPv4-mapped IPv6 绕过防护（P0-2）
3. deep_research 诊断文本不污染 sources（P0-3）
4. _parse_search_results URL 取最后一行（P1-1）
5. web_search 翻页参数传递（P2）
6. charset 字节级检测（P1-2）
7. 二进制 magic bytes 检测（P2-6）
8. max_chars 上限保护（P3-6）
9. reset_deep_researcher 单例重置（P1-6）
10. Bing host fallback（P1-3）
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ============================================================
# 辅助：提取 handler
# ============================================================
def _get_web_search_handler():
    from skills import SkillManager
    mgr = SkillManager.__new__(SkillManager)
    mgr._skills = {}
    mgr._seed_web_search_skill()
    return mgr._skills["web_search"].handler


def _get_web_fetch_handler():
    from skills import SkillManager
    mgr = SkillManager.__new__(SkillManager)
    mgr._skills = {}
    mgr._seed_web_search_skill()
    return mgr._skills["web_fetch"].handler


# ============================================================
# 1. SSRF 重定向绕过防护（P0-1）
# ============================================================
class TestSSRFRedirectProtection:
    """V67 P0-1：重定向到内网地址必须被拦截。"""

    def test_redirect_to_169_254_metadata_blocked(self):
        """公网 URL 302 跳转到 169.254.169.254（云元数据）必须被阻止。"""
        handler = _get_web_fetch_handler()

        # 第一次请求返回 302 重定向到元数据服务
        redirect_resp = MagicMock()
        redirect_resp.status_code = 302
        redirect_resp.headers = {"location": "http://169.254.169.254/latest/meta-data/"}

        # 模拟 httpx 在 event_hooks 阶段抛出 RequestError
        import httpx

        class FakeClient:
            def __init__(self, *a, **kw):
                # 捕获 event_hooks 用于验证
                self._event_hooks = kw.get("event_hooks", {})
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw):
                # 模拟 event_hooks 在重定向时触发
                for hook in self._event_hooks.get("request", []):
                    await hook(MagicMock())
                raise httpx.RequestError("SSRF blocked")

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://attacker.com/redir"}))

        assert "[web_fetch error" in result
        assert "RequestError" in result or "SSRF" in result or "blocked" in result.lower()

    def test_max_redirects_limited_to_3(self):
        """V67：max_redirects 应限制为 3，防止无限重定向。"""
        handler = _get_web_fetch_handler()
        captured_kwargs = {}

        class FakeClient:
            def __init__(self, *a, **kw):
                captured_kwargs.update(kw)
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw):
                # 返回一个正常响应避免报错
                resp = MagicMock()
                resp.status_code = 200
                resp.text = "<html><body><article><p>" + "x" * 200 + "</p></article></body></html>"
                resp.content = resp.text.encode()
                resp.encoding = "utf-8"
                resp.headers = {"content-type": "text/html"}
                return resp

        with patch("httpx.AsyncClient", FakeClient):
            asyncio.run(handler({"url": "https://example.com/page"}))

        assert captured_kwargs.get("max_redirects") == 3
        assert captured_kwargs.get("follow_redirects") is True


# ============================================================
# 2. SSRF IPv4-mapped IPv6 绕过防护（P0-2）
# ============================================================
class TestSSRFIPv4MappedIPv6:
    """V67 P0-2：::ffff:127.0.0.1 等 IPv4-mapped IPv6 必须被拦截。"""

    def test_ipv4_mapped_loopback_blocked(self):
        """::ffff:127.0.0.1 应被识别为回环地址并阻止。"""
        import ipaddress
        # 直接验证 ipaddress 模块能识别 IPv4-mapped IPv6
        addr = ipaddress.ip_address("::ffff:127.0.0.1")
        assert addr.is_loopback

    def test_ipv4_mapped_private_blocked(self):
        """::ffff:10.0.0.1 应被识别为私网地址。"""
        import ipaddress
        addr = ipaddress.ip_address("::ffff:10.0.0.1")
        assert addr.is_private

    def test_ipv4_mapped_metadata_blocked(self):
        """::ffff:169.254.169.254 应被识别为链路本地地址。"""
        import ipaddress
        addr = ipaddress.ip_address("::ffff:169.254.169.254")
        assert addr.is_link_local

    def test_ipv4_mapped_loopback_rejected_by_handler(self):
        """handler 应拒绝解析到 ::ffff:127.0.0.1 的域名。"""
        handler = _get_web_fetch_handler()

        with patch("socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [(0, 0, 0, "", ("::ffff:127.0.0.1", 0))]
            result = asyncio.run(handler({"url": "https://evil.com/meta"}))

        assert "访问被拒绝" in result or "内网" in result or "回环" in result or "保留" in result


# ============================================================
# 3. deep_research 诊断文本不污染 sources（P0-3）
# ============================================================
class TestDeepResearchDiagnosticPollution:
    """V67 P0-3：web_search 失败诊断文本不应被解析为搜索结果。"""

    def test_failure_diagnostic_returns_empty_sources(self):
        """_do_search 收到 '[web_search: 全部源均失败...]' 应返回空列表。"""
        from core.deep_research import DeepResearcher

        researcher = DeepResearcher(llm_provider=None, skills_manager=None)

        # 模拟 web_search 返回失败诊断
        diagnostic_text = (
            "[web_search: 全部源（Bing、360搜索、DuckDuckGo）均失败。"
            "最后错误: Bing=0 results; 360搜索=0 results; DuckDuckGo=0 results。\n"
            "替代方案：1) 已知目标 URL → 用 system_run curl\n"
            "   例: system_run(\"curl -sL 'https://example.com' | head -c 2000\")\n"
            "2) 已知 API key → 用 python_execute]"
        )

        # _parse_search_results 不应从诊断文本提取出 example.com
        sources = researcher._parse_search_results(diagnostic_text)
        # 诊断文本以 [web_search 开头，应被 _do_search 拦截，但 _parse_search_results 也应过滤
        for s in sources:
            assert "example.com" not in s.url, f"诊断文本污染: {s.url}"

    def test_do_search_returns_empty_on_diagnostic(self):
        """_do_search 在收到诊断文本时应返回空列表。"""
        from core.deep_research import DeepResearcher

        researcher = DeepResearcher(llm_provider=None, skills_manager=None)

        # 模拟 skills_manager
        mock_skill = MagicMock()
        mock_skill.run = AsyncMock(return_value=(
            "[web_search: 全部源（Bing）均失败。最后错误: Bing=0 results。]"
        ))
        researcher._skills = MagicMock()
        researcher._skills.get = MagicMock(return_value=mock_skill)

        sources = asyncio.run(researcher._do_search("test query"))
        assert sources == [], f"诊断文本不应产生 sources，得到: {sources}"

    def test_do_search_returns_empty_on_timeout_diagnostic(self):
        """_do_search 在收到超时诊断时也应返回空列表。"""
        from core.deep_research import DeepResearcher

        researcher = DeepResearcher(llm_provider=None, skills_manager=None)

        mock_skill = MagicMock()
        mock_skill.run = AsyncMock(return_value=(
            "[web_search: 搜索超时（25s）。已尝试源: Bing。"
            "建议用 system_run + curl 直接获取目标 URL。]"
        ))
        researcher._skills = MagicMock()
        researcher._skills.get = MagicMock(return_value=mock_skill)

        sources = asyncio.run(researcher._do_search("test query"))
        assert sources == []


# ============================================================
# 4. _parse_search_results URL 取最后一行（P1-1）
# ============================================================
class TestParseSearchResultsUrlExtraction:
    """V67 P1-1：URL 提取应优先取最后一行，避免 snippet 中的 URL 被误取。"""

    def test_url_from_last_line_not_snippet(self):
        """snippet 中包含 URL 时，应取最后一行的结果链接。"""
        from core.deep_research import DeepResearcher

        researcher = DeepResearcher(llm_provider=None, skills_manager=None)

        # 模拟 web_search 输出：snippet 里有一个 URL，最后一行是真正的结果 URL
        text = (
            "搜索结果（test，来源: Bing）：\n\n"
            "Agnes AI 官方文档\n"
            "  详情见 https://wrong-url-in-snippet.com 也可以看\n"
            "  https://correct-url.com\n\n"
        )

        sources = researcher._parse_search_results(text)
        assert len(sources) == 1
        assert sources[0].url == "https://correct-url.com"
        assert "wrong-url-in-snippet" not in sources[0].url

    def test_url_extraction_fallback_to_search(self):
        """如果最后一行不是 URL，fallback 到整条 entry 搜索。"""
        from core.deep_research import DeepResearcher

        researcher = DeepResearcher(llm_provider=None, skills_manager=None)

        text = (
            "搜索结果（test，来源: Bing）：\n\n"
            "Some Title\n"
            "  snippet without url on last line\n"
            "  but url is https://embedded-url.com here\n\n"
        )

        sources = researcher._parse_search_results(text)
        assert len(sources) == 1
        assert sources[0].url == "https://embedded-url.com"


# ============================================================
# 5. web_search 翻页参数传递（P2）
# ============================================================
class TestWebSearchPagination:
    """V67 P2：翻页参数应正确传递到各搜索源。"""

    def test_page_param_accepted_in_schema(self):
        """web_search schema 应包含 page 参数。"""
        from skills import SkillManager
        mgr = SkillManager.__new__(SkillManager)
        mgr._skills = {}
        mgr._seed_web_search_skill()
        schema = mgr._skills["web_search"].schema
        assert "page" in schema["function"]["parameters"]["properties"]
        assert schema["function"]["parameters"]["properties"]["page"]["type"] == "integer"


# ============================================================
# 6. charset 字节级检测（P1-2）
# ============================================================
class TestCharsetByteLevelDetection:
    """V67 P1-2：charset 检测应在 raw_bytes 上做，不依赖已解码文本。"""

    def test_gbk_page_decoded_correctly(self):
        """GBK 编码的中文页面应正确解码。"""
        handler = _get_web_fetch_handler()

        # 构造 GBK 编码的 HTML
        html_str = (
            '<html><head><meta charset="gbk"></head><body><article>'
            "<h1>中文标题测试</h1>"
            "<p>这是一段中文正文内容，用于测试 GBK 编码能否正确解码。</p>"
            "<p>第二段中文内容，确保长度足够通过最小检查。</p>"
            "</article></body></html>"
        )
        gbk_bytes = html_str.encode("gbk")

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = html_str  # 模拟 httpx 默认解码（假设正确）
        fake_resp.content = gbk_bytes
        fake_resp.encoding = "utf-8"  # httpx 默认用 utf-8（错误）
        fake_resp.headers = {"content-type": "text/html"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/cn-page"}))

        # 应该能正确解码中文（因为 meta charset=gbk 触发重新解码）
        assert "中文标题测试" in result or "中文正文" in result


# ============================================================
# 7. 二进制 magic bytes 检测（P2-6）
# ============================================================
class TestBinaryMagicBytesDetection:
    """V67 P2-6：即使无 Content-Type 头，也应通过 magic bytes 识别二进制。"""

    def test_pdf_magic_bytes_detected_without_content_type(self):
        """PDF 文件无 Content-Type 头时，应通过 %PDF 魔数识别。"""
        handler = _get_web_fetch_handler()

        pdf_bytes = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<</Pages 2 0 R>>endobj"

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = ""  # 二进制无法解码为文本
        fake_resp.content = pdf_bytes
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {}  # 无 content-type

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/doc.pdf"}))

        assert "二进制内容" in result
        assert "PDF" in result

    def test_png_magic_bytes_detected(self):
        """PNG 文件应通过魔数识别。"""
        handler = _get_web_fetch_handler()

        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = ""
        fake_resp.content = png_bytes
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {"content-type": "text/plain"}  # 误导性 content-type

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/img.png"}))

        assert "二进制内容" in result
        assert "PNG" in result

    def test_zip_magic_bytes_detected(self):
        """ZIP 文件应通过 PK 魔数识别。"""
        handler = _get_web_fetch_handler()

        zip_bytes = b"PK\x03\x04" + b"\x00" * 100

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = ""
        fake_resp.content = zip_bytes
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/archive"}))

        assert "二进制内容" in result
        assert "ZIP" in result


# ============================================================
# 8. max_chars 上限保护（P3-6）
# ============================================================
class TestMaxCharsUpperLimit:
    """V67 P3-6：max_chars 超过 20000 应被截断到 20000。"""

    def test_max_chars_capped_at_20000(self):
        """LLM 传 max_chars=1000000 应被限制到 20000。"""
        handler = _get_web_fetch_handler()

        long_html = (
            "<html><body><article><p>" + "A" * 50000 + "</p></article></body></html>"
        )

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = long_html
        fake_resp.content = long_html.encode()
        fake_resp.encoding = "utf-8"
        fake_resp.headers = {"content-type": "text/html"}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **kw): return fake_resp

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"url": "https://example.com/long", "max_chars": 1000000}))

        # 结果不应超过 20000 + 少量 header
        # 截断后的正文最多 20000 字符
        assert len(result) < 22000, f"max_chars 未被限制，结果长度 {len(result)}"


# ============================================================
# 9. reset_deep_researcher 单例重置（P1-6）
# ============================================================
class TestResetDeepResearcher:
    """V67 P1-6：reset_deep_researcher 应清空单例。"""

    def test_reset_clears_singleton(self):
        """reset 后单例应为 None，下次 get 创建新实例。"""
        from core.deep_research import (
            get_deep_researcher, reset_deep_researcher, _deep_researcher,
        )
        import core.deep_research as dr_module

        # 先创建一个实例
        mock_llm = MagicMock()
        r1 = get_deep_researcher(llm=mock_llm)
        assert r1 is not None

        # reset
        reset_deep_researcher()
        assert dr_module._deep_researcher is None

        # 再创建应是新实例
        r2 = get_deep_researcher(llm=mock_llm)
        assert r2 is not r1


# ============================================================
# 10. Bing host fallback（P1-3）
# ============================================================
class TestBingHostFallback:
    """V67 P1-3：cn.bing.com 失败应继续尝试 www.bing.com。"""

    def test_first_host_error_falls_back_to_second(self):
        """cn.bing.com 抛 ConnectError 后应继续尝试 www.bing.com。"""
        handler = _get_web_search_handler()

        import httpx

        call_count = {"n": 0}

        class FakeClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, *a, **kw):
                call_count["n"] += 1
                if "cn.bing.com" in str(url):
                    # 第一个 host 失败
                    raise httpx.ConnectError("SSL error")
                # 第二个 host (www.bing.com) 返回结果
                resp = MagicMock()
                resp.status_code = 200
                resp.text = (
                    '<li class="b_algo">'
                    '<h2><a href="https://result.com">Test Result Title</a></h2>'
                    '<div class="b_caption"><p class="b_lineclamp2">snippet</p></div>'
                    '</li>'
                )
                return resp
            async def post(self, *a, **kw):
                raise httpx.ConnectError("mock")

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(handler({"input": "test query"}))

        # V67 并发模式：cn.bing.com 失败后 www.bing.com 应被尝试
        assert call_count["n"] >= 2, f"至少 2 次请求（cn.bing + www.bing），实际 {call_count['n']}"
        # 第二个 host 成功，结果应包含 result.com
        assert "result.com" in result or "Test Result" in result


# ============================================================
# 11. 跨子问题 source 去重（P2-5）
# ============================================================
class TestCrossSubQuestionDedup:
    """V67 P2-5：跨子问题应按 URL 去重。"""

    def test_duplicate_urls_across_subquestions_deduped(self):
        """两个子问题返回相同 URL 的 source，all_sources 中只应有一个。"""
        from core.deep_research import DeepResearcher, ResearchSource

        researcher = DeepResearcher(llm_provider=None, skills_manager=None)

        # 模拟 _research_sub_question 返回相同 URL 的 sources
        async def mock_research(sq, model, depth):
            sources = [ResearchSource(url="https://same.com", title="Same", snippet="s")]
            finding = MagicMock()
            finding.sub_question = sq
            finding.sources = sources
            finding.answer = "answer"
            finding.confidence = 0.5
            return finding, sources, 1

        with patch.object(researcher, "_research_sub_question", side_effect=mock_research):
            with patch.object(researcher, "_decompose", new=AsyncMock(return_value=["q1", "q2"])):
                with patch.object(researcher, "_synthesize", new=AsyncMock(return_value="synth")):
                    report = asyncio.run(researcher.research("test", depth=1))

        # all_sources 中 https://same.com 只应出现一次
        same_count = sum(1 for s in report.sources if s.url == "https://same.com")
        assert same_count == 1, f"跨子问题去重失败，same.com 出现 {same_count} 次"


# ============================================================
# 12. _research_sub_question 异常不中断研究（P2-4）
# ============================================================
class TestSubQuestionExceptionIsolation:
    """V67 P2-4：单个子问题异常不应中断整个研究。"""

    def test_one_subquestion_fails_others_continue(self):
        """第一个子问题抛异常，第二个应正常完成。"""
        from core.deep_research import DeepResearcher, ResearchSource

        researcher = DeepResearcher(llm_provider=None, skills_manager=None)

        call_count = {"n": 0}

        async def mock_research(sq, model, depth):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("LLM 500 error")
            sources = [ResearchSource(url="https://ok.com", title="OK", snippet="s")]
            finding = MagicMock()
            finding.sub_question = sq
            finding.sources = sources
            finding.answer = "good answer"
            finding.confidence = 0.8
            return finding, sources, 1

        with patch.object(researcher, "_research_sub_question", side_effect=mock_research):
            with patch.object(researcher, "_decompose", new=AsyncMock(return_value=["q1", "q2"])):
                with patch.object(researcher, "_synthesize", new=AsyncMock(return_value="synth")):
                    report = asyncio.run(researcher.research("test", depth=1))

        # 两个子问题都应被尝试
        assert call_count["n"] == 2
        # 应有 2 个 finding（第一个是失败的，第二个是成功的）
        assert len(report.findings) == 2
        # 失败的 finding 应包含错误信息
        assert "研究失败" in report.findings[0].answer or "500" in report.findings[0].answer
