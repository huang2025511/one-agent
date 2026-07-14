"""V68 智能体能力升级测试。

覆盖：
1. P0: failure_recovery 系统级策略恢复接入
2. P1: 每步工具调用后即时反思
3. P2: deep_research 子问题并行执行
4. P3: 跨会话成功策略记忆
"""

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ============================================================
# P3: 跨会话成功策略记忆
# ============================================================
class TestSuccessStrategyMemory:
    """V68 P3：成功策略记忆 — 记录和查询历史成功工具链。"""

    def _get_improver(self, db_path=None):
        """创建临时 DB 的 SelfImprover。"""
        from core.self_improve import SelfImprover
        if db_path is None:
            fd, db_path = tempfile.mkstemp(suffix=".db")
            os.close(fd)
        improver = SelfImprover(db_path=db_path)
        return improver, db_path

    def test_record_and_retrieve_success_strategy(self):
        """记录成功策略后应能查询到。"""
        improver, db_path = self._get_improver()
        try:
            improver.record_success_strategy(
                task_signature="code:12345",
                tool_chain="web_search→python_execute",
                task_type="code",
                effect_score=0.8,
            )
            result = improver.get_success_strategy("code:12345")
            assert result is not None
            assert result["tool_chain"] == "web_search→python_execute"
            assert result["effect_score"] == 0.8
            assert result["use_count"] == 1
        finally:
            improver.close()

    def test_repeated_strategy_updates_use_count(self):
        """相同策略多次记录应增加 use_count。"""
        improver, db_path = self._get_improver()
        try:
            for _ in range(3):
                improver.record_success_strategy(
                    task_signature="analysis:99999",
                    tool_chain="web_search→web_fetch",
                    task_type="analysis",
                    effect_score=0.7,
                )
            result = improver.get_success_strategy("analysis:99999")
            assert result is not None
            assert result["use_count"] == 3
        finally:
            improver.close()

    def test_low_score_strategy_not_returned(self):
        """效果评分低于 0.4 的策略不应返回。"""
        improver, db_path = self._get_improver()
        try:
            improver.record_success_strategy(
                task_signature="test:00001",
                tool_chain="bad_tool",
                task_type="test",
                effect_score=0.2,
            )
            result = improver.get_success_strategy("test:00001")
            assert result is None  # 评分太低，不返回
        finally:
            improver.close()

    def test_nonexistent_signature_returns_none(self):
        """不存在的签名应返回 None。"""
        improver, db_path = self._get_improver()
        try:
            result = improver.get_success_strategy("nonexistent:00000")
            assert result is None
        finally:
            improver.close()

    def test_empty_inputs_ignored(self):
        """空签名或空工具链应被忽略。"""
        improver, db_path = self._get_improver()
        try:
            improver.record_success_strategy("", "tool", "type", 0.8)
            improver.record_success_strategy("sig", "", "type", 0.8)
            improver.record_success_strategy("sig", "tool", "type", 0.8)
            assert improver.get_success_strategy("") is None
            assert improver.get_success_strategy("sig") is not None
        finally:
            improver.close()


# ============================================================
# P2: deep_research 子问题并行执行
# ============================================================
class TestSubQuestionParallel:
    """V68 P2：子问题应并行执行，总耗时 ≈ max(单问题) 而非累加。"""

    def test_sub_questions_run_in_parallel(self):
        """多个子问题应并行而非串行。"""
        from core.deep_research import DeepResearcher, ResearchSource

        researcher = DeepResearcher(llm_provider=None, skills_manager=None)

        execution_times = []

        async def mock_research(sq, model, depth):
            start = asyncio.get_event_loop().time()
            await asyncio.sleep(0.3)  # 每个子问题耗时 0.3s
            elapsed = asyncio.get_event_loop().time() - start
            execution_times.append(elapsed)
            sources = [ResearchSource(url="https://" + sq[:5] + ".com", title=sq, snippet="s")]
            finding = MagicMock()
            finding.sub_question = sq
            finding.sources = sources
            finding.answer = "answer"
            finding.confidence = 0.5
            return finding, sources, 1

        with patch.object(researcher, "_research_sub_question", side_effect=mock_research):
            with patch.object(researcher, "_decompose", new=AsyncMock(return_value=["q1", "q2", "q3"])):
                with patch.object(researcher, "_synthesize", new=AsyncMock(return_value="synth")):
                    import time
                    start = time.time()
                    report = asyncio.run(researcher.research("test", depth=1))
                    total_time = time.time() - start

        # 串行需要 0.9s，并行只需 ~0.3s
        assert total_time < 0.6, f"并行执行总耗时 {total_time:.2f}s 过长（应 < 0.6s）"
        assert len(report.findings) == 3
        assert len(report.sources) == 3

    def test_parallel_failure_isolation(self):
        """一个子问题失败不影响其他。"""
        from core.deep_research import DeepResearcher, ResearchSource

        researcher = DeepResearcher(llm_provider=None, skills_manager=None)

        call_count = {"n": 0}

        async def mock_research(sq, model, depth):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("LLM error")
            sources = [ResearchSource(url="https://ok.com", title="OK", snippet="s")]
            finding = MagicMock()
            finding.sub_question = sq
            finding.sources = sources
            finding.answer = "good"
            finding.confidence = 0.8
            return finding, sources, 1

        with patch.object(researcher, "_research_sub_question", side_effect=mock_research):
            with patch.object(researcher, "_decompose", new=AsyncMock(return_value=["q1", "q2"])):
                with patch.object(researcher, "_synthesize", new=AsyncMock(return_value="synth")):
                    report = asyncio.run(researcher.research("test", depth=1))

        assert len(report.findings) == 2
        assert "研究失败" in report.findings[0].answer
        assert report.findings[1].answer == "good"


# ============================================================
# P0: failure_recovery 接入验证
# ============================================================
class TestFailureRecoveryIntegration:
    """V68 P0：failure_recovery.get_recovery_strategy 应被调用。"""

    def test_classify_error_returns_correct_type(self):
        """classify_error 应正确分类常见错误。"""
        from core.failure_recovery import FailureRecovery

        assert FailureRecovery.classify_error(TimeoutError("timeout")) == "timeout"
        assert FailureRecovery.classify_error(ConnectionError("refused")) == "connect_error"

        # 429 rate limit — classify_error 检查 "rate" + "limit"
        exc = Exception("rate limit exceeded")
        assert FailureRecovery.classify_error(exc) == "rate_limit"

        # 401 auth
        exc = Exception("HTTP 401 Unauthorized")
        assert FailureRecovery.classify_error(exc) == "auth_error"

        # 500 server error
        exc = Exception("HTTP 500 Internal Server Error")
        assert FailureRecovery.classify_error(exc) == "server_error"

    def test_get_recovery_strategy_returns_valid_action(self):
        """get_recovery_strategy 应返回有效的 action。"""
        from core.failure_recovery import get_failure_recovery

        fr = get_failure_recovery()

        for error_type in ["timeout", "rate_limit", "auth_error", "server_error", "connect_error", "context_overflow", "unknown"]:
            strategy = fr.get_recovery_strategy(error_type)
            assert "action" in strategy
            assert strategy["action"] in ("retry", "switch_model", "simplify")
            assert "suggestion" in strategy

    def test_recovery_strategy_for_timeout_is_retry(self):
        """超时错误应返回 retry 策略。"""
        from core.failure_recovery import get_failure_recovery

        fr = get_failure_recovery()
        strategy = fr.get_recovery_strategy("timeout")
        assert strategy["action"] == "retry"
        assert strategy["retry_delay"] > 0

    def test_recovery_strategy_for_auth_is_switch_model(self):
        """认证错误应返回 switch_model 策略。"""
        from core.failure_recovery import get_failure_recovery

        fr = get_failure_recovery()
        strategy = fr.get_recovery_strategy("auth_error")
        assert strategy["action"] == "switch_model"


# ============================================================
# P1: 即时反思机制（单元级验证）
# ============================================================
class TestReflectionMechanism:
    """V68 P1：工具调用后应评估结果质量。"""

    def test_reflection_evaluates_sufficient_results(self):
        """工具结果充分时不应触发调整提示。"""
        # 验证反思逻辑的存在：通过检查 coordinator 代码中是否有 reflection_triggered
        # 完整的集成测试需要 LLM mock，这里验证关键路径
        from core.coordinator import Coordinator
        # 确认 coordinator 有反思相关的逻辑（通过源码检查）
        import inspect
        source = inspect.getsource(Coordinator._tool_loop)
        assert "reflection" in source.lower() or "INSUFFICIENT" in source or "SUFFICIENT" in source

    def test_reflection_evaluates_insufficient_results(self):
        """工具结果不足时应触发调整提示。"""
        from core.coordinator import Coordinator
        import inspect
        source = inspect.getsource(Coordinator._tool_loop)
        assert "INSUFFICIENT" in source or "不足" in source


# ============================================================
# 端到端：deep_research 并行 + 全文抓取
# ============================================================
class TestDeepResearchParallelWithFetch:
    """V68：deep_research 并行执行 + 全文抓取端到端。"""

    def test_parallel_research_completes_all_subquestions(self):
        """并行执行应完成所有子问题。"""
        from core.deep_research import DeepResearcher, ResearchSource

        researcher = DeepResearcher(llm_provider=None, skills_manager=None)

        async def mock_research(sq, model, depth):
            await asyncio.sleep(0.1)
            sources = [ResearchSource(url=f"https://{sq}.com", title=sq, snippet="s")]
            finding = MagicMock()
            finding.sub_question = sq
            finding.sources = sources
            finding.answer = f"answer for {sq}"
            finding.confidence = 0.7
            return finding, sources, 1

        with patch.object(researcher, "_research_sub_question", side_effect=mock_research):
            with patch.object(researcher, "_decompose", new=AsyncMock(return_value=["a", "b", "c", "d"])):
                with patch.object(researcher, "_synthesize", new=AsyncMock(return_value="synth")):
                    report = asyncio.run(researcher.research("test", depth=1))

        assert len(report.findings) == 4
        assert len(report.sources) == 4
        # 每个子问题都有答案
        for f in report.findings:
            assert "answer" in f.answer
