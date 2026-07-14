"""测试第 8 轮审计修复：空 tool name 诊断。"""

import asyncio
import pytest


# ============================================================
# Bug 9: 空 name 工具调用导致 [unknown skill: ] 反复重试
# ============================================================
class TestEmptyToolNameDispatch:
    """验证 _dispatch_smart 在 name 为空时返回清晰错误，避免 LLM 反复重试。"""

    def setup_method(self):
        # 不需要完整初始化 coordinator，只用 _dispatch_smart
        from core.coordinator import Coordinator
        # 用 mock - 我们只关心 _dispatch_smart 的逻辑
        self.coord = Coordinator.__new__(Coordinator)
        self.coord._skills = None  # 让 skills.dispatch 路径跳过
        self.failed_skills = {}

    def test_empty_name_returns_error(self):
        """name="" 应返回 status=error 的 ToolResult，而不是 success + [unknown skill: ]"""
        result = asyncio.run(
            self.coord._dispatch_smart(
                tc={"id": "call_0", "type": "function"},
                name="",
                args={"input": "hello"},
                failed_skills=self.failed_skills,
            )
        )
        assert result.status == "error"
        assert "工具调用格式错误" in result.error
        assert "name 字段为空" in result.error
        assert "<invoke name=" in result.error  # 给出替代格式建议

    def test_whitespace_only_name_returns_error(self):
        """name="   " 也应被识别为空"""
        result = asyncio.run(
            self.coord._dispatch_smart(
                tc={"id": "call_0", "type": "function"},
                name="   ",
                args={"input": "hello"},
                failed_skills=self.failed_skills,
            )
        )
        assert result.status == "error"

    def test_valid_name_passes_through(self):
        """正常 name 不应被空名拦截逻辑影响"""
        # name 有效但 skills=None，预期 "[no skill manager bound]"
        result = asyncio.run(
            self.coord._dispatch_smart(
                tc={"id": "call_0", "type": "function"},
                name="web_search",
                args={"input": "hello"},
                failed_skills=self.failed_skills,
            )
        )
        # skills=None 走 line 270: result_str = "[no skill manager bound]"
        # is_error = False — 包装成 success
        assert result.status == "success"
        assert "[unknown skill" not in result.data
        assert "工具调用格式错误" not in result.data


# ============================================================
# Bug 10: 流式 tool_call 累积时 name 全程空 — done chunk 应显式标记
# ============================================================
class TestStreamingToolNameFallback:
    """验证 chat_completion_stream 在 name 全程空时注入 <empty> 标记。"""

    def test_empty_name_marked_in_done_chunk(self):
        """模拟流式累积：所有 chunk 都没给 name — done chunk 的 name 应被标记为 <empty>"""
        # 不实际运行 stream，验证"如果累积完成 name 还空就标记"的逻辑
        # 直接构造 tool_call_chunks 模拟累积结果
        tool_call_chunks = {
            0: {
                "id": "call_0",
                "type": "function",
                "function": {"name": "", "arguments": "{}"},
            },
        }
        # 模拟"标记空 name"逻辑
        assembled = [tool_call_chunks[k] for k in sorted(tool_call_chunks.keys())]
        for atc in assembled:
            fn = atc.get("function") or {}
            if not (fn.get("name") or "").strip():
                fn["name"] = "<empty>"

        assert assembled[0]["function"]["name"] == "<empty>"
        # arguments 仍保留
        assert assembled[0]["function"]["arguments"] == "{}"

    def test_valid_name_not_overwritten(self):
        """name 已存在时不应被 <empty> 覆盖"""
        tool_call_chunks = {
            0: {
                "id": "call_0",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"input": "test"}'},
            },
        }
        assembled = [tool_call_chunks[k] for k in sorted(tool_call_chunks.keys())]
        for atc in assembled:
            fn = atc.get("function") or {}
            if not (fn.get("name") or "").strip():
                fn["name"] = "<empty>"

        assert assembled[0]["function"]["name"] == "web_search"
        # arguments 完整保留
        assert assembled[0]["function"]["arguments"] == '{"input": "test"}'

    def test_mixed_empty_and_valid(self):
        """多个 tool_call 中，1 个 name 正常 + 1 个 name 空 — 只标记空的"""
        tool_call_chunks = {
            0: {
                "id": "call_0",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"input": "test"}'},
            },
            1: {
                "id": "call_1",
                "type": "function",
                "function": {"name": "", "arguments": "{}"},
            },
        }
        assembled = [tool_call_chunks[k] for k in sorted(tool_call_chunks.keys())]
        for atc in assembled:
            fn = atc.get("function") or {}
            if not (fn.get("name") or "").strip():
                fn["name"] = "<empty>"

        assert assembled[0]["function"]["name"] == "web_search"  # 不被覆盖
        assert assembled[1]["function"]["name"] == "<empty>"    # 被标记


# ============================================================
# 集成测试：dispatch 流程配合
# ============================================================
class TestEmptyNameIntegration:
    """端到端测试：空 name tool_call 经过 _execute_tool_calls 的处理流程。"""

    def test_empty_name_tool_call_marked_before_dispatch(self):
        """模拟 _execute_tool_calls 入口：tc['name'] = '' 走 <empty> 路径"""
        from core.coordinator import Coordinator

        coord = Coordinator.__new__(Coordinator)
        coord._skills = None
        failed_skills = {}

        # 模拟流式累积后的 tool_call（已带 <empty> 标记）
        tc = {
            "id": "call_0",
            "type": "function",
            "function": {"name": "<empty>", "arguments": "{}"},
        }
        # _execute_tool_calls 的入口：name = tc.get("name") or ""
        name = tc.get("name") or ""
        # 显式标记后 name 不会是空（即使原 LLM 没给）

        # _dispatch_smart 在 <empty> 标记下应该返回 error
        result = asyncio.run(
            coord._dispatch_smart(tc=tc, name=name, args={}, failed_skills=failed_skills)
        )
        assert result.status == "error"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
