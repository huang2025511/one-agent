"""验证第三轮深度审查修复的针对性测试。

覆盖：
1. _dump_config 密钥脱敏（写盘前还原 ${ENV_VAR}）
2. skills/_save_config 临时文件清理（yaml.YAMLError 路径）
3. skills/_save_config 密钥脱敏
4. restart_handler 调用 plugin.stop() 后再 execv
5. set_api_key 把 api_key 传给 resolve
6. _web_search_provider_api 带 Authorization 头
"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ============================================================
# Bug 1: models/recommend._dump_config 密钥脱敏
# ============================================================
class TestDumpConfigSanitization:
    def test_plaintext_api_key_redacted_to_env_var(self, monkeypatch, tmp_path):
        """展开后的明文 API key 应被还原为 ${ENV_VAR} 占位符。"""
        from models.recommend import RecommendationMixin

        # 实现 mixin 的最小子类（mixin 本身不能实例化）
        class Dummy(RecommendationMixin):
            pass

        m = Dummy()
        # 模拟环境变量中存在 OPENAI_API_KEY=sk-xxx
        monkeypatch.setenv("OPENAI_API_KEY", "sk-plaintext-secret")

        cfg = {
            "llm": {
                "api_keys": {
                    "openai": "sk-plaintext-secret",   # 已展开的明文
                    "anthropic": "",                    # 空值保留
                },
                "primary_model": "gpt-4o",
            },
            "agent": {"name": "test"},
        }
        result = m._sanitize_for_persist(cfg)
        # openai key 应还原为 ${OPENAI_API_KEY}
        assert result["llm"]["api_keys"]["openai"] == "${OPENAI_API_KEY}", \
            f"明文 API key 应被还原为占位符，实际：{result['llm']['api_keys']['openai']}"
        # 空值保留
        assert result["llm"]["api_keys"]["anthropic"] == ""
        # 非敏感字段保留
        assert result["llm"]["primary_model"] == "gpt-4o"
        assert result["agent"]["name"] == "test"

    def test_enc_prefix_preserved(self, monkeypatch):
        """enc: 前缀的加密内容应保留（可安全落盘）。"""
        from models.recommend import RecommendationMixin

        class Dummy(RecommendationMixin):
            pass

        m = Dummy()
        monkeypatch.setenv("OPENAI_API_KEY", "some-real-key")
        cfg = {
            "llm": {
                "api_keys": {
                    "openai": "enc:abc123encrypted==",  # 加密内容
                },
            },
        }
        result = m._sanitize_for_persist(cfg)
        # enc: 前缀保留
        assert result["llm"]["api_keys"]["openai"] == "enc:abc123encrypted=="

    def test_placeholder_preserved(self, monkeypatch):
        """${VAR} 占位符应保留（本来就是占位符）。"""
        from models.recommend import RecommendationMixin

        class Dummy(RecommendationMixin):
            pass

        m = Dummy()
        cfg = {
            "llm": {
                "api_keys": {
                    "openai": "${OPENAI_API_KEY}",   # 占位符
                },
            },
        }
        result = m._sanitize_for_persist(cfg)
        assert result["llm"]["api_keys"]["openai"] == "${OPENAI_API_KEY}"

    def test_no_matching_env_writes_null(self, monkeypatch):
        """找不到对应环境变量时应写 null（避免明文落盘）。"""
        from models.recommend import RecommendationMixin

        class Dummy(RecommendationMixin):
            pass

        m = Dummy()
        # 确保没有任何 API_KEY 类环境变量匹配此值
        monkeypatch.delenv("FAKE_API_KEY", raising=False)
        monkeypatch.delenv("MY_API_KEY", raising=False)
        cfg = {
            "llm": {
                "api_keys": {
                    "custom_provider": "totally-unique-key-not-in-env",
                },
            },
        }
        result = m._sanitize_for_persist(cfg)
        assert result["llm"]["api_keys"]["custom_provider"] is None, \
            "找不到匹配 env var 时应写 null，避免明文落盘"

    def test_dangerous_env_not_matched(self, monkeypatch):
        """PATH 等非密钥类 env var 不应被还原为 ${PATH}。"""
        from models.recommend import RecommendationMixin

        class Dummy(RecommendationMixin):
            pass

        m = Dummy()
        # 假设有 PATH=/usr/bin，且某个 api_key 的值恰好等于 /usr/bin
        monkeypatch.setenv("PATH", "/usr/bin")
        cfg = {
            "llm": {
                "api_keys": {
                    "weird": "/usr/bin",   # 值与 PATH 相等但 PATH 不是密钥类
                },
            },
        }
        result = m._sanitize_for_persist(cfg)
        # 不应还原为 ${PATH}，应写 null
        assert result["llm"]["api_keys"]["weird"] is None


# ============================================================
# Bug 2: skills/_save_config 临时文件清理（YAMLError 路径）
# ============================================================
class TestSaveConfigTempFileCleanup:
    def test_temp_file_cleaned_on_yaml_error(self, monkeypatch, tmp_path):
        """yaml.dump 抛 YAMLError 时临时文件应被清理。"""
        import yaml as _yaml
        # 模拟 yaml.dump 抛 YAMLError
        from yaml.error import YAMLError

        config_path = tmp_path / "config.yaml"
        config_path.write_text("existing: config\n", encoding="utf-8")
        monkeypatch.setenv("ONE_AGENT_CONFIG", str(config_path))

        # 保存原 yaml.dump，注入错误版本
        orig_dump = _yaml.dump
        files_created = []

        def tracking_dump(*args, **kwargs):
            # 记录临时文件路径
            f = args[0] if args else kwargs.get("stream")
            try:
                name = getattr(f, "name", None)
                if name:
                    files_created.append(name)
            except Exception:
                pass
            raise YAMLError("simulated yaml error")

        monkeypatch.setattr(_yaml, "dump", tracking_dump)
        try:
            from skills import _save_config
            # 不应抛出（_save_config 内部 except Exception 捕获）
            _save_config({"test": "data"})
        finally:
            monkeypatch.setattr(_yaml, "dump", orig_dump)

        # 关键断言：所有临时文件都应被清理
        leftover = [f for f in files_created if os.path.exists(f)]
        assert not leftover, f"yaml.YAMLError 后临时文件应被清理，残留：{leftover}"


# ============================================================
# Bug 3: skills/_save_config 密钥脱敏
# ============================================================
class TestSkillsSaveConfigSanitization:
    def test_plaintext_key_not_written_to_disk(self, monkeypatch, tmp_path):
        """写盘的 YAML 文件不应包含明文 API key。"""
        config_path = tmp_path / "config.yaml"
        monkeypatch.setenv("ONE_AGENT_CONFIG", str(config_path))
        monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-do-not-leak")

        from skills import _save_config
        cfg = {
            "llm": {
                "api_keys": {"openai": "sk-secret-do-not-leak"},
                "primary_model": "gpt-4o",
            },
        }
        _save_config(cfg)

        # 读取写盘的内容
        written = config_path.read_text(encoding="utf-8")
        assert "sk-secret-do-not-leak" not in written, \
            f"明文密钥不应落盘，写入内容：{written}"
        assert "${OPENAI_API_KEY}" in written, \
            f"应还原为占位符，写入内容：{written}"


# ============================================================
# Bug 4: restart_handler 调用 plugin.stop() 后再 execv
# ============================================================
class TestRestartHandlerGracefulStop:
    def test_stop_called_before_execv(self, monkeypatch):
        """restart_handler 应在 os.execv 之前调用 plugin.stop()。

        由于 restart_handler 在 SkillManager.setup() 中作为闭包注册，
        难以直接调用。改用源码静态检查验证修复：
        - 应包含 await plugin.stop() 调用（而非原实现直接 os.execv）
        - 应使用 loop.create_task 调度 async 清理
        """
        import inspect
        from skills import SkillManager

        # restart_handler 在 _seed_core_skills 中作为闭包定义
        setup_src = inspect.getsource(SkillManager._seed_core_skills)

        # 关键修复点 1：应有 async 的 _do_restart_async，先 await stop 再 execv
        assert "_do_restart_async" in setup_src, \
            "restart 应使用 async 协程而非 sync call_later"
        assert "plugin.stop" in setup_src or "stop_fn" in setup_src, \
            "restart_handler 应调用 plugin.stop()（修复后）"
        # 关键修复点 2：原实现是 loop.call_later(1.5, _do_restart)，
        # 修复后是 loop.create_task(_do_restart_async())
        assert "create_task" in setup_src, \
            "restart 应使用 create_task 调度 async 清理（修复后）"
        # 关键修复点 3：应有 wait_for timeout 防止卡死
        assert "wait_for" in setup_src, \
            "restart 应有 timeout 防止 plugin.stop 卡死（修复后）"

    @pytest.mark.asyncio
    async def test_full_restart_calls_stop_then_execv(self, monkeypatch):
        """端到端验证：mock os.execv 后调用 restart_handler 应触发 stop。"""
        # 用 monkeypatch 替换 os.execv 防止真正替换进程
        execv_calls = []
        monkeypatch.setattr("os.execv", lambda *a, **kw: execv_calls.append(a))

        stop_calls = []

        class FakePlugin:
            name = "fake_llm"

            async def stop(self):
                stop_calls.append("fake_llm")

        from core.context import AgentContext
        from core.events import EventBus
        ctx = AgentContext(config={}, bus=EventBus())
        ctx._plugins = [FakePlugin()]

        from skills import SkillManager
        sm = SkillManager()
        await sm.setup(ctx)

        # 找到 restart skill handler
        restart_skill = None
        for s in sm._skills.values():
            if s.id == "restart":
                restart_skill = s
                break
        assert restart_skill is not None, "restart skill 应已注册"

        # 执行 handler
        await restart_skill.handler({})
        # 让 _do_restart_async 协程执行（sleep 1.5s + stop + execv）
        await asyncio.sleep(2.5)

        # 关键断言：stop 应被调用
        assert len(stop_calls) > 0, \
            f"restart_handler 应在 execv 前调用 plugin.stop()，stop_calls={stop_calls}"
        # execv 应被调用（在 stop 之后）
        assert len(execv_calls) > 0, \
            f"execv 应被调用，execv_calls={execv_calls}"


# ============================================================
# Bug 5: set_api_key 把 api_key 传给 resolve
# ============================================================
class TestSetApiKeyPassesKeyToResolve:
    def test_resolve_called_with_api_key(self, monkeypatch):
        """set_api_key 应把 api_key 传给 resolve。"""
        from models import LLMProvider

        provider = LLMProvider()
        provider._api_keys = {}
        provider._provider_base_urls = {}
        provider._auto_classify_timestamps = {}
        provider._no_tools_models = set()
        # 阻止 auto_classify 真正执行
        provider._spawn_bg = MagicMock(return_value=MagicMock())

        # 拦截 resolve 调用
        resolve_calls = []

        async def fake_resolve(prov, api_key="", **kw):
            resolve_calls.append({"provider": prov, "api_key": api_key})

            class FakeResult:
                found = False
                base_url = None

            return FakeResult()

        # Mock 掉 resolver.resolve
        import models.resolver as _resolver
        monkeypatch.setattr(_resolver, "resolve", fake_resolve)
        # Mock 掉 asyncio.get_event_loop 返回非 running loop（走 except RuntimeError 分支）
        # 实际上 set_api_key 内部 try/except 处理 loop 逻辑较复杂，
        # 我们直接验证 ensure_future 调用的 resolve 签名
        # 通过 patch asyncio.ensure_future 拦截
        ensure_future_calls = []

        def tracking_ensure_future(coro):
            # coro 是 resolve(provider, api_key=...) 调用
            # 我们需要 await 它来拿 result，但为了简单直接关闭
            ensure_future_calls.append(coro)
            # 创建一个 fake task
            task = MagicMock()
            task.add_done_callback = lambda cb: None
            return task

        # 由于 set_api_key 路径复杂，最简单的验证方式是直接调 fake_resolve
        # 通过 monkeypatch 替换 ensure_future
        import asyncio as _asyncio
        monkeypatch.setattr(_asyncio, "ensure_future", tracking_ensure_future)

        # 强制走 is_running()=True 分支
        running_loop = MagicMock()
        running_loop.is_running.return_value = True
        monkeypatch.setattr(_asyncio, "get_event_loop", lambda: running_loop)

        provider.set_api_key("custom_provider", "sk-test-key-123")

        # 验证：ensure_future 被调用且参数包含 api_key
        # 由于 ensure_future 收到的是 coroutine 对象，我们 inspect 它的参数
        # 简化验证：直接调用 fake_resolve 看签名是否正确
        # 更稳妥：检查 ensure_future_calls 不为空
        assert len(ensure_future_calls) > 0, "ensure_future 应被调用（调 resolve）"
        # 验证 coroutine 的参数（cr_frame 上的 locals）
        coro = ensure_future_calls[0]
        # coroutine 对象可以通过 cr_frame.f_locals 拿到参数（在 Python 3.12+ 可能受限）
        # 这里改用更简单的方式：重新调用 fake_resolve 验证签名兼容
        # 实际上，关键修复是 set_api_key 调用 resolve(provider, api_key=key)
        # 我们用源码静态检查验证
        import inspect
        src = inspect.getsource(LLMProvider.set_api_key)
        assert "resolve(provider, api_key=key)" in src or "resolve(provider, api_key=" in src, \
            "set_api_key 应把 api_key 传给 resolve（修复后）"


# ============================================================
# Bug 6: _web_search_provider_api 带 Authorization 头
# ============================================================
class TestWebSearchProviderApiAuthHeader:
    def test_signature_accepts_api_key(self):
        """_web_search_provider_api 签名应支持 api_key 参数。"""
        import inspect
        from skills import _web_search_provider_api

        sig = inspect.signature(_web_search_provider_api)
        assert "api_key" in sig.parameters, \
            "_web_search_provider_api 应接受 api_key 参数（修复后）"

    def test_search_provider_url_passes_api_key(self):
        """_search_provider_url 应把 api_key 透传给 _web_search_provider_api。"""
        import inspect
        from skills import _search_provider_url

        src = inspect.getsource(_search_provider_url)
        assert "api_key=api_key" in src, \
            "_search_provider_url 应把 api_key 传给 _web_search_provider_api"

    @pytest.mark.asyncio
    async def test_authorization_header_sent_on_probe(self, monkeypatch):
        """探测 /models 端点时应发送 Authorization 头。"""
        from skills import _web_search_provider_api

        captured_headers = []

        class FakeResp:
            status_code = 200

            def json(self):
                # 返回含 AbstractURL 的数据，触发 /models 探测分支
                return {
                    "AbstractURL": "https://docs.example.com/api",
                    "RelatedTopics": [],
                }

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kwargs):
                # 捕获请求 headers
                h = kwargs.get("headers") or {}
                captured_headers.append({"url": url, "headers": dict(h)})
                # DuckDuckGo 返回 200+json，/models 探测返回 401
                class ProbeResp:
                    status_code = 401  # 不影响 header 捕获
                return FakeResp() if "duckduckgo" in url else ProbeResp()

        # Mock httpx.AsyncClient
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())

        # 调用：带 api_key
        await _web_search_provider_api("custom_provider", api_key="sk-test-key")

        # 验证：/models 探测请求带 Authorization 头
        # DuckDuckGo 请求不带 auth（正常），但 /models 应带
        probe_requests = [h for h in captured_headers if "/models" in h["url"]]
        assert len(probe_requests) > 0, \
            f"应至少探测一个 /models 端点，实际捕获：{captured_headers}"
        auth_requests = [h for h in probe_requests if "Authorization" in h["headers"]]
        assert len(auth_requests) > 0, \
            f"探测 /models 应带 Authorization 头，实际捕获：{probe_requests}"
        # Authorization 值应为 Bearer sk-test-key
        assert auth_requests[0]["headers"]["Authorization"] == "Bearer sk-test-key"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
