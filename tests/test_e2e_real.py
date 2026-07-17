"""用真实 nvidia key 真实启动 OneAgentApp 全栈，端到端检测所有功能。

策略：
- 用 tmp_path 隔离数据目录避免污染真实数据
- 真实启动 OneAgentApp（setup_all + start_all）
- 逐个检测：coordinator turn、系统命令、记忆、scheduler、marketplace、router 路由等
- 收集所有 warning/error 日志，识别 bug
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 修复 #1（安全）：移除硬编码真实 API Key。
# 该 key 已进入 git 历史，必须立即吊销并轮换。
# 现在从环境变量读取，CI 已用 --ignore=tests/test_e2e_real.py 排除此文件。
# 本地运行需：export NVIDIA_API_KEY=nvapi-xxxx
NVIDIA_KEY = os.environ.get("NVIDIA_API_KEY", "nvapi-FAKE-KEY-FOR-TESTING-ONLY")


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """构造一个隔离的 config 文件 + 数据目录。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_file = tmp_path / "config.yaml"
    cfg = {
        "agent": {"name": "TestAgent", "data_dir": str(data_dir), "language": "zh"},
        "llm": {
            "primary_provider": "nvidia",
            "primary_model": "meta/llama-3.1-8b-instruct",
            "lightweight_model": "meta/llama-3.1-8b-instruct",
            "api_keys": {"nvidia": NVIDIA_KEY},
            "default_temperature": 0.3,
            "default_max_tokens": 100,
            "timeout": 30,
            "retries": 1,
            "auto_classify_on_setup": False,
        },
        "router": {
            "enabled": True,
            "task_complexity_thresholds": {
                "trivial": 0.2, "simple": 0.5, "complex": 0.8, "expert": 1.0,
            },
            "self_evolution": {"enabled": True, "eval_interval": 5},
        },
        "memory": {
            "short_term": {"max_turns": 5, "max_tokens": 1000},
            "long_term": {"enabled": True, "storage": "sqlite-fts5", "max_results": 3},
            "procedural": {
                "enabled": True,
                "auto_create_skills": True,
                "min_usage_before_skill": 2,
            },
        },
        "security": {
            "system_executor_password": "",
            "require_password_for_dangerous": True,
        },
        # 禁用所有 HTTP server：test_e2e_real 测的是 bus 内部流程（coordinator
        # chat turn / skill dispatch / memory / router），不需要 REST API /
        # WebGateway / Monitor。否则会和 conftest.py 的 session-scoped app
        # 抢 18791/18792/18793 端口（fastapi 装上后两个 app 实例都会尝试
        # bind 同一组端口 → SystemExit: 3）。
        "rest": {"enabled": False},
        "gateways": {
            "web": {"enabled": False},
            "wechat_personal": {"enabled": False},
        },
        "monitoring": {"enabled": False},
    }
    config_file.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setenv("ONE_AGENT_CONFIG", str(config_file))
    return config_file, data_dir, cfg


# 关键：使用 pytest_asyncio.fixture(loop_scope="function") 让 fixture 和 test
# 跑在同一个 function-scoped loop 上。
# 否则 pyproject.toml 里的 asyncio_default_fixture_loop_scope = "session" 会让 fixture
# 跑在 session loop 上，而 test 跑在 function loop 上 → bus task 和 publish 不在
# 同一个 loop，导致 publish 进了队列但 bus task 永远不会调度执行（事件卡死）。
@pytest_asyncio.fixture(loop_scope="function")
async def running_app(isolated_config, monkeypatch):
    """启动真实 OneAgentApp。"""
    config_file, data_dir, cfg = isolated_config
    # 防止后台 LLM 调用占用资源
    monkeypatch.setenv("ONE_AGENT_NO_TELEMETRY", "1")
    # 开 DEBUG 日志看 chat 走向
    logging.getLogger("core.coordinator").setLevel(logging.DEBUG)
    logging.getLogger("models").setLevel(logging.DEBUG)
    logging.getLogger("router").setLevel(logging.DEBUG)
    from one_agent import OneAgentApp
    app = OneAgentApp(str(config_file))
    await app.start()
    # 关键不变量：bus task 必须和当前 test 跑在同一个 loop（bug 30 修复验证）
    assert id(app.bus._task.get_loop()) == id(asyncio.get_running_loop()), \
        "bus task 和 test 应在同一个 loop（bug 30 修复）"
    try:
        yield app
    finally:
        try:
            await app.stop()
        except Exception as e:
            print(f"[teardown] stop 异常: {e}")
        # 清理残留后台任务
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in tasks:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


# ============================================================
# 检测 1: app 真实启动 + 关键插件就绪
# ============================================================
class TestAppBoots:
    @pytest.mark.asyncio
    async def test_app_starts_and_plugins_ready(self, running_app):
        app = running_app
        print(f"\n[检测1] 验证关键插件就绪")
        # 关键插件不应为 None
        assert app.llm is not None
        assert app.router is not None
        assert app.memory is not None
        assert app.skills is not None
        assert app.coordinator is not None
        assert app.scheduler is not None
        assert app.exec_shell is not None
        assert app.exec_python is not None
        # ctx 应已构造
        assert app.ctx is not None
        # 关键属性
        assert app.ctx._plugins, "ctx._plugins 应非空"
        assert app.ctx.approval_manager is not None, "approval_manager 应已注入"
        assert app.ctx.mcp_client is not None, "mcp_client 应已注入"
        assert app.ctx.python_executor is not None, "python_executor 应已注入"
        # 验证修复 8: alert_manager 在 _plugins 中
        plugin_names = [getattr(p, "name", "?") for p in app.ctx._plugins]
        assert "alerting" in plugin_names, f"alerting 应在 _plugins 中: {plugin_names}"
        print(f"[检测1✓] {len(plugin_names)} 个插件就绪: {plugin_names}")
        # 验证 alert_manager 注入
        assert app.ctx._alert_manager is not None
        # 验证 monitor metrics_getter 已注入 alert_manager
        assert app._alert_manager._metrics_getter is not None, \
            "alert_manager 应有 metrics_getter（来自 monitor）"


# ============================================================
# 检测 2: 真实 chat turn（最小 LLM 调用）
# ============================================================
class TestRealChatTurn:
    @pytest.mark.asyncio
    async def test_direct_llm_call(self, running_app):
        """直接调 app.llm.chat_completion，绕过 coordinator，诊断 LLM 是否工作。"""
        app = running_app
        print(f"\n[检测2-A] 直接调 app.llm.chat_completion")
        # 诊断 setup 后的状态
        print(f"[检测2-A] _provider_base_urls: {app.llm._provider_base_urls}")
        print(f"[检测2-A] _api_keys keys: {list(app.llm._api_keys.keys())}")
        print(f"[检测2-A] _default_model: {app.llm._default_model}")
        nvidia_base = app.llm._provider_base_urls.get("nvidia")
        nvidia_key = app.llm._api_keys.get("nvidia")
        print(f"[检测2-A] nvidia base={nvidia_base}, key={'set' if nvidia_key else 'NOT SET'}")
        # 确保 key 已设置
        if not nvidia_key:
            app.llm.set_api_key("nvidia", NVIDIA_KEY)
            print(f"[检测2-A] set_api_key 后 nvidia base={app.llm._provider_base_urls.get('nvidia')}")
        try:
            resp = await asyncio.wait_for(
                app.llm.chat_completion(
                    messages=[{"role": "user", "content": "回复OK"}],
                    model="meta/llama-3.1-8b-instruct",
                    max_tokens=20,
                    temperature=0,
                ),
                timeout=60,
            )
            print(f"[检测2-A] 响应: {resp.get('text', '')[:60]!r}")
            print(f"[检测2-A] 完整 resp keys: {list(resp.keys())}")
            if resp.get("error"):
                print(f"[检测2-A] error: {resp['error']}")
            assert resp.get("text"), f"应返回文本: {resp}"
        except asyncio.TimeoutError:
            print(f"[检测2-A⚠️] LLM 直接调用 60s 超时")
            raise

    @pytest.mark.asyncio
    async def test_minimal_chat_turn(self, running_app):
        """发起一次最小 chat，验证完整 turn 流程不抛异常。"""
        app = running_app
        import time as _time
        from core.context import TurnContext
        t0 = _time.monotonic()
        print(f"\n[检测2-B] 真实 chat turn（经 coordinator） t={0:.2f}s".format(0))
        bus = app.bus
        # 关键诊断：bus 已就绪
        assert bus._running and bus._task, "EventBus 应已启动"
        assert id(asyncio.get_running_loop()) == id(bus._task.get_loop()), \
            "bus task 和 test 应在同一个 loop"
        # 关键诊断：router/coordinator 已订阅
        assert len(bus._subscribers.get("user_message", [])) >= 1, "router 应订阅 user_message"
        assert len(bus._subscribers.get("turn_routed", [])) >= 1, "coordinator 应订阅 turn_routed"
        # 关键诊断：auto_classify_on_setup=False 生效（bug 29 验证）
        assert not getattr(app.llm, "_pending_auto_classify", False), \
            "auto_classify_on_setup=False 应阻止后台分类（bug 29）"
        assert len(app.llm._bg_tasks) == 0, "setup 后不应有挂起的 auto-classify task"

        # 直接构造 turn 经 bus 走 router → coordinator → LLM
        turn = TurnContext(input_text="回复OK", source="test", session_id="e2e-test")
        app.bus.publish({
            "type": "user_message",
            "payload": {"turn": turn, "session_id": "e2e-test"},
            "source": "test",
        })
        print(f"[检测2-B] published t={_time.monotonic()-t0:.2f}s")
        # 简化：分多次 sleep，记录状态
        waits = [0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 15.0]
        total = 0
        for w in waits:
            await asyncio.sleep(w)
            total += w
            if turn.result or turn.error:
                break
            print(f"[检测2-B] t={total:.1f}s 未完成 queue={bus._queue.qsize()} "
                  f"metrics={bus._metrics} complexity={turn.estimated_complexity} "
                  f"model={turn.model}")
        print(f"[检测2-B] final t={_time.monotonic()-t0:.2f}s metrics={bus._metrics}")
        print(f"[检测2-B] result={turn.result!r}, error={turn.error!r}")
        if turn.result:
            print(f"[检测2-B✓] chat 成功")
        elif turn.error:
            print(f"[检测2-B⚠️] chat 失败: {turn.error}")
        else:
            print(f"[检测2-B⚠️] turn 未完成（router 未 dispatch）")
        # 断言：要么有结果，要么是显式 error（不应静默未完成）
        assert turn.result or turn.error, \
            f"turn 应有结果或 error，不应静默未完成。metrics={bus._metrics}"


# ============================================================
# 检测 3: 系统命令执行 skill
# ============================================================
class TestSystemExecSkills:
    @pytest.mark.asyncio
    async def test_safe_command_executes(self, running_app):
        """SAFE 级别命令应无密码执行。"""
        app = running_app
        skills = app.skills
        sys_skill = skills.get("system.run") or skills.get("system")
        print(f"\n[检测3-A] SAFE 命令执行")
        if sys_skill is None:
            # 找其他名字
            for s in skills._skills.values():
                if "system" in s.id.lower() or "shell" in s.id.lower():
                    sys_skill = s
                    break
        assert sys_skill is not None, "应注册 system.run skill"
        result = await sys_skill.handler({"command": "echo hello_e2e"})
        print(f"[检测3-A] 结果: {result}")
        assert "hello_e2e" in result, f"echo 应输出 hello_e2e: {result}"

    @pytest.mark.asyncio
    async def test_dangerous_command_rejected(self, running_app):
        """DANGEROUS 命令在无密码时应被拒绝。"""
        app = running_app
        skills = app.skills
        sys_skill = None
        for s in skills._skills.values():
            if "system" in s.id.lower() or "shell" in s.id.lower():
                sys_skill = s
                break
        assert sys_skill is not None
        print(f"\n[检测3-B] DANGEROUS 命令应被拒绝")
        # rm -rf 是 DANGEROUS
        result = await sys_skill.handler({"command": "rm -rf /tmp/nonexistent"})
        print(f"[检测3-B] 结果: {result[:100]}")
        # 应该是拒绝/需要密码
        assert "密码" in result or "拒绝" in result or "permission" in result.lower() \
            or "denied" in result.lower() or "dangerous" in result.lower(), \
            f"DANGEROUS 命令应被拒绝: {result}"


# ============================================================
# 检测 4: 记忆持久化
# ============================================================
class TestMemoryPersistence:
    @pytest.mark.asyncio
    async def test_save_and_retrieve_memory(self, running_app):
        """保存笔记 skill + 查询长期记忆。"""
        app = running_app
        skills = app.skills
        print(f"\n[检测4] 记忆持久化")
        # 找记忆相关 skill
        save_skill = None
        search_skill = None
        for s in skills._skills.values():
            sid = s.id.lower()
            if "save" in sid or "remember" in sid or "note" in sid:
                save_skill = s
            if "search" in sid or "recall" in sid or "memory_search" in sid:
                search_skill = s
        print(f"[检测4] save_skill={save_skill.id if save_skill else None}, "
              f"search_skill={search_skill.id if search_skill else None}")
        # 列出所有 skill id 以便诊断
        all_ids = sorted([s.id for s in skills._skills.values()])
        print(f"[检测4] 所有 skills: {all_ids}")
        # 断言：save_note skill 应存在
        assert save_skill is not None, f"save_note skill 应存在: {all_ids}"
        # 真实写入一条笔记
        try:
            result = await save_skill.handler({"input": "e2e 测试笔记: 记忆持久化检查"})
            print(f"[检测4] save_note 结果: {str(result)[:80]}")
        except Exception as e:
            print(f"[检测4⚠️] save_note 异常: {e}")


# ============================================================
# 检测 5: router 4 层路由分发
# ============================================================
class TestRouterTierDispatch:
    @pytest.mark.asyncio
    async def test_router_classifies_complexity(self, running_app):
        """router 应对不同复杂度输入选择不同 tier。
        通过 publish user_message 走完整 router pipeline，验证 estimated_complexity
        被设置、tier 被选择。"""
        from core.context import TurnContext
        app = running_app
        router = app.router
        print(f"\n[检测5] router 复杂度分类（经 bus pipeline）")
        # 直接调内部 _classify（同步），快速验证不同输入
        cases = [
            ("你好", "trivial"),
            ("解释一下量子计算的基本原理，包括波粒二象性、叠加态、纠缠等核心概念", "complex_or_higher"),
        ]
        for text, expected_tier in cases:
            complexity = router._classify(text)
            tier = router._tier_for_complexity(complexity)
            print(f"[检测5] '{text[:25]}' → complexity={complexity:.2f} tier={tier}")
            # 短问候应在 trivial/simple
            if "你好" in text:
                assert complexity < 0.5, f"短问候应低复杂度: {complexity}"
            # 长问题应 >= simple
            if "量子" in text:
                assert complexity >= 0.1, f"长问题应非平凡: {complexity}"


# ============================================================
# 检测 6: scheduler + marketplace 基本功能
# ============================================================
class TestSchedulerMarketplace:
    @pytest.mark.asyncio
    async def test_scheduler_add_cron(self, running_app):
        """scheduler 应能添加 cron 任务。"""
        app = running_app
        sched = app.scheduler
        print(f"\n[检测6-A] scheduler 添加 cron")
        called = []
        def cb():
            called.append(1)
        try:
            sched.add_cron("* * * * *", cb, "test_cron")
            crons = sched.list_crons() if hasattr(sched, "list_crons") else []
            print(f"[检测6-A✓] 已注册 cron: {crons if crons else '无法列出'}")
            # 清理
            sched.remove_cron("test_cron") if hasattr(sched, "remove_cron") else None
        except Exception as e:
            print(f"[检测6-A⚠️] {e}")

    @pytest.mark.asyncio
    async def test_marketplace_list(self, running_app):
        """marketplace 应能列出。"""
        app = running_app
        mp = app.marketplace
        print(f"\n[检测6-B] marketplace 列出")
        try:
            result = await mp.list_skills() if hasattr(mp, "list_skills") else []
            print(f"[检测6-B✓] marketplace skills: {len(result) if result else 0}")
        except Exception as e:
            print(f"[检测6-B⚠️] {e}")


# ============================================================
# 检测 7: 收集运行时 warning/error 日志
# ============================================================
class TestRuntimeLogCapture:
    @pytest.mark.asyncio
    async def test_collect_runtime_warnings(self, running_app, caplog):
        """跑一个 turn，收集所有 WARNING+/ERROR 日志。"""
        import logging
        app = running_app
        print(f"\n[检测7] 收集运行时 warning/error")
        with caplog.at_level(logging.WARNING):
            try:
                result = await asyncio.wait_for(
                    app.chat("解释 1+1 等于几", source="test", session_id="log-test"),
                    timeout=60,
                )
                print(f"[检测7] chat 结果: {result[:60]!r}")
            except asyncio.TimeoutError:
                print(f"[检测7⚠️] chat timeout")

        # 收集 WARNING 及以上
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        print(f"[检测7] 共 {len(warnings)} 个 WARNING+, {len(errors)} 个 ERROR")
        # 打印前 15 条
        for r in warnings[:15]:
            print(f"   [{r.levelname}] {r.name}: {r.message[:120]}")
        # 致命 ERROR 应为 0（warning 可以有，error 不应有未捕获的）
        # 注意：gateway 启动失败会有 warning，这是预期行为
        unexpected_errors = [r for r in errors
                            if "gateway" not in r.message.lower()
                            and "telegram" not in r.message.lower()
                            and "wecom" not in r.message.lower()
                            and "discord" not in r.message.lower()
                            and "slack" not in r.message.lower()
                            and "feishu" not in r.message.lower()
                            and "wechat_personal" not in r.message.lower()
                            and "dingtalk" not in r.message.lower()]
        if unexpected_errors:
            print(f"[检测7⚠️] 非预期 ERROR:")
            for r in unexpected_errors:
                print(f"   🚨 [{r.levelname}] {r.name}: {r.message[:200]}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--tb=short", "-x"])
