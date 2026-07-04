"""验证深度审查修复的针对性测试。

每个测试对应一个具体修复，确保语义正确。
"""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ============================================================
# Bug 4: catalog.auto_classify_tier — 付费 mini 不应判为 trivial
# ============================================================
class TestAutoClassifyTierPaidMini:
    def _make(self, id_, is_free, ctx=8000, features=None, name=None):
        from models.catalog import ModelInfo
        return ModelInfo(
            id=id_,
            name=name or id_,
            provider="test",
            description="",
            created=0,
            context_length=ctx,
            max_output_length=0,
            input_modalities=["text"],
            output_modalities=["text"],
            pricing={},
            features=features or [],
            quantization="",
            is_free=is_free,
            tags=[],
            raw={},
        )

    def test_paid_mini_goes_simple_not_trivial(self):
        """付费 mini 模型（如 gpt-4o-mini）应进 simple，不是 trivial。"""
        from models.catalog import auto_classify_tier
        m = self._make("openai/gpt-4o-mini", is_free=False, ctx=128000)
        assert auto_classify_tier(m) != "trivial", \
            "付费 mini 模型不应被判为 trivial（修复前 bug）"
        # 应该是 simple 或 complex（取决于上下文大小和特性）
        assert auto_classify_tier(m) in ("simple", "complex")

    def test_free_mini_still_trivial(self):
        """免费 mini 模型仍应进 trivial。"""
        from models.catalog import auto_classify_tier
        m = self._make("some/mini-1b", is_free=True, ctx=4000)
        assert auto_classify_tier(m) == "trivial"


# ============================================================
# Bug 5: catalog._normalize — is_free=False 不应被 pricing 覆盖
# ============================================================
class TestNormalizeIsFreeOverride:
    def test_explicit_is_free_false_not_overridden_by_pricing(self):
        from models.catalog import ModelCatalog
        cat = ModelCatalog.__new__(ModelCatalog)
        cat.provider = "test"
        item = {
            "id": "paid-model",
            "is_free": False,
            "pricing": {"prompt": "0"},  # 价格=0 通常会被推断为免费
        }
        info = cat._normalize(item, model_id="paid-model")
        assert info.is_free is False, \
            "API 明确声明 is_free=False，不应被 pricing 推断覆盖"

    def test_explicit_is_free_true_preserved(self):
        from models.catalog import ModelCatalog
        cat = ModelCatalog.__new__(ModelCatalog)
        cat.provider = "test"
        item = {
            "id": "free-model",
            "is_free": True,
            "pricing": {"prompt": "0.01"},  # 有价格但 is_free=True
        }
        info = cat._normalize(item, model_id="free-model")
        assert info.is_free is True

    def test_no_is_free_uses_pricing(self):
        from models.catalog import ModelCatalog
        cat = ModelCatalog.__new__(ModelCatalog)
        cat.provider = "test"
        item = {
            "id": "unknown-model",
            "pricing": {"prompt": "0"},  # 价格=0 推断为免费
        }
        info = cat._normalize(item, model_id="unknown-model")
        assert info.is_free is True


# ============================================================
# Bug 7: skills._model_cost_desc — 未登记模型不应判为免费
# ============================================================
class TestModelCostDescUnknown:
    def test_unknown_model_returns_unknown_not_free(self):
        from skills import _model_cost_desc
        # 用一个几乎不可能在 MODEL_COST 里的模型 ID
        result = _model_cost_desc("nonexistent-provider/totally-fake-model-xyz123")
        assert result == "未知", f"未登记模型应返回'未知'，实际: {result}"

    def test_free_model_still_returns_free(self):
        from skills import _model_cost_desc
        from models.tiers import MODEL_COST
        # 找一个明确 cost=0 的模型
        free_model = next((k for k, v in MODEL_COST.items() if v == 0), None)
        if free_model:
            assert _model_cost_desc(free_model) == "免费"


# ============================================================
# Bug 6: memory._min_usage — 达到门槛才创建技能
# ============================================================
class TestMinUsageBeforeSkill:
    def _make_plugin(self, tmp_path):
        from memory import MemoryPlugin
        p = MemoryPlugin()
        p._min_usage = 3
        p._auto_create_skills = True
        # _long 必须 non-None，否则 _on_turn_completed 会早返回跳过 skill 创建
        p._long = MagicMock()
        p._long.add = MagicMock(return_value=1)
        p._embeddings = None
        p._kg = None
        p._procedural = MagicMock()
        p._procedural.lookup.return_value = None  # 没有已存在技能
        p._procedural.save = MagicMock()
        p._pending_skills = {}
        p._recent_memory_hashes = {}
        p._dedup_window = 60.0
        return p

    def _make_teachable_turn(self, text="请帮我写一个 Python 排序算法"):
        # 构造 _looks_teachable 返回 True 的 turn
        return SimpleNamespace(
            input_text=text,
            result="```python\ndef sort(x): return sorted(x)\n```" + "x" * 200,
            error=None,
            source="test",
            session_id="test-1",
        )

    @pytest.mark.asyncio
    async def test_skill_not_created_below_threshold(self):
        from core.events import Event
        p = self._make_plugin("/tmp")
        # 用 3 个不同 turn 模拟 3 次独立交互（同 trigger 词 "排序"）
        turns = [
            self._make_teachable_turn(f"请帮我写一个 Python 排序算法 v{i}")
            for i in range(2)
        ]
        for turn in turns:
            await p._on_turn_completed(Event("turn_completed", {"turn": turn}))
        assert p._procedural.save.call_count == 0, \
            "未达到 _min_usage 次不应创建技能"

    @pytest.mark.asyncio
    async def test_skill_created_at_threshold(self):
        from core.events import Event
        p = self._make_plugin("/tmp")
        # 3 次独立交互（不同 input_text，同 trigger 词 "排序"）
        turns = [
            self._make_teachable_turn(f"请帮我写一个 Python 排序算法 v{i}")
            for i in range(3)
        ]
        for turn in turns:
            await p._on_turn_completed(Event("turn_completed", {"turn": turn}))
        assert p._procedural.save.call_count == 1, \
            "达到 _min_usage 次应创建技能"


# ============================================================
# Bug 2: coordinator._compress_context — 保留原始系统提示词
# ============================================================
class TestCompressContextPreservesSystemPrompt:
    @pytest.mark.asyncio
    async def test_original_system_prompt_preserved(self):
        from core.coordinator import Coordinator
        from core.context import TurnContext

        coord = Coordinator()
        coord._llm = None  # 跳过真实 LLM 调用

        original_system = {"role": "system", "content": "你是 One-Agent，使用智能路由..."}
        messages = [
            original_system,
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，有什么可以帮你？"},
            {"role": "user", "content": "再做点事" + "x" * 5000},
            {"role": "assistant", "content": "好的" + "y" * 5000},
            {"role": "user", "content": "继续" + "z" * 5000},
        ]

        # mock _compress_messages 返回非空摘要以触发压缩
        async def fake_compress(msgs, turn):
            return "这是摘要"
        coord._compress_messages = fake_compress

        turn = SimpleNamespace(
            model="test",
            meta={},
            session_id="test-1",
        )
        # 设置 ctx 以通过 compression_enabled 检查
        coord.ctx = SimpleNamespace(config={
            "router": {"context_compression": {"enabled": True}},
            "memory": {"short_term": {"max_tokens": 100}},  # 故意小，触发压缩
        })

        await coord._compress_context(messages, turn)

        # 关键断言：messages[0] 仍然是原始系统提示词
        assert messages[0] is original_system, \
            "压缩后 messages[0] 必须保留原始系统提示词"
        assert "智能路由" in messages[0]["content"]
        # 摘要应作为第二个 system 消息
        assert any("对话历史摘要" in m.get("content", "") for m in messages), \
            "应包含摘要 system 消息"
        assert turn.meta.get("context_compressed") is True


# ============================================================
# Bug 3: coordinator._multi_agent_phase — KeyError 防御
# ============================================================
class TestMultiAgentPhaseKeyError:
    @pytest.mark.asyncio
    async def test_missing_total_tokens_no_keyerror(self):
        from core.coordinator import Coordinator
        from core.context import TurnContext

        coord = Coordinator()
        coord._llm = MagicMock()

        # mock DelegationManager 返回缺 total_tokens/subtasks 的 result
        class FakeDelegator:
            async def execute(self, *a, **kw):
                # 故意缺 total_tokens 和 subtasks
                return {"parallel": True, "result": "答案"}

        # mock publish 和 record_success
        coord.publish = MagicMock()
        coord.ctx = None

        turn = SimpleNamespace(
            input_text="复杂任务",
            model="test",
            result="",
            meta={},
            session_id="test-1",
            record_success=MagicMock(),
        )

        # patch DelegationManager
        import core.sub_agent
        orig = core.sub_agent.DelegationManager if hasattr(core.sub_agent, 'DelegationManager') else None

        import core.coordinator as coord_mod
        # 通过 monkey patch sys.modules 注入
        import sys as _sys
        fake_mod = MagicMock()
        fake_mod.DelegationManager = lambda llm, sk: FakeDelegator()
        _sys.modules['core.sub_agent'] = fake_mod

        try:
            result = await coord._multi_agent_phase([], turn)
            assert result is True, "应成功处理"
            assert turn.meta["delegation_total_tokens"] == 0
            assert turn.meta["subtask_count"] == 0
        finally:
            # 恢复
            if orig is not None:
                _sys.modules['core.sub_agent'] = core.sub_agent
            else:
                del _sys.modules['core.sub_agent']


# ============================================================
# Bug 10: events.handler_errors — 应记录异常信息
# ============================================================
class TestEventHandlerErrorsMessages:
    @pytest.mark.asyncio
    async def test_error_message_contains_exception(self):
        from core.events import EventBus, Event

        bus = EventBus()

        def bad_handler(event):
            raise ValueError("specific_test_error_xyz")

        bus.subscribe("test_event_xyz", bad_handler)
        event = Event("test_event_xyz", {"x": 1})
        await bus._dispatch(event)

        # 应标记为 failed（仅 1 个 handler）
        assert event.error is not None
        assert "specific_test_error_xyz" in event.error, \
            f"error 应包含异常信息，实际: {event.error}"
        # 不应包含 "<function" 字符串
        assert "<function" not in event.error, \
            f"不应记录函数 repr，实际: {event.error}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
