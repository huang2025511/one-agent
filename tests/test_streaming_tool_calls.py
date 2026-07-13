"""Test streaming tool_calls accumulation logic (mirrors models/__init__.py)."""
import json


def accumulate_tool_calls(chunks):
    """Mirror the logic in chat_completion_stream OpenAI path."""
    tool_call_chunks = {}
    for data in chunks:
        choices = data.get("choices") or []
        if choices:
            delta = choices[0].get("delta", {})
            delta_tool_calls = delta.get("tool_calls") or []
            for tc in delta_tool_calls:
                idx = tc.get("index", 0)
                if idx not in tool_call_chunks:
                    tool_call_chunks[idx] = {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                entry = tool_call_chunks[idx]
                if tc.get("id"):
                    entry["id"] = tc["id"]
                if tc.get("function", {}).get("name"):
                    entry["function"]["name"] += tc["function"]["name"]
                if tc.get("function", {}).get("arguments"):
                    entry["function"]["arguments"] += tc["function"]["arguments"]
    return [tool_call_chunks[k] for k in sorted(tool_call_chunks.keys())]


def test_single_tool_call_fragmented_args():
    """Tool call with arguments split across multiple chunks."""
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_abc123", "type": "function",
             "function": {"name": "web_search", "arguments": ""}}
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"input"'}}
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": ': "hello world"}'}}
        ]}}]},
        {"choices": [], "usage": {"total_tokens": 150}},
    ]
    assembled = accumulate_tool_calls(chunks)
    assert len(assembled) == 1
    assert assembled[0]["id"] == "call_abc123"
    assert assembled[0]["type"] == "function"
    assert assembled[0]["function"]["name"] == "web_search"
    assert assembled[0]["function"]["arguments"] == '{"input": "hello world"}'


def test_multiple_tool_calls_single_chunk():
    """Two tool calls delivered in the same chunk."""
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "web_search", "arguments": '{"input":"q1"}'}},
            {"index": 1, "id": "call_2", "function": {"name": "calc", "arguments": '{"input":"1+1"}'}},
        ]}}]},
        {"choices": [], "usage": {"total_tokens": 100}},
    ]
    assembled = accumulate_tool_calls(chunks)
    assert len(assembled) == 2
    assert assembled[0]["function"]["name"] == "web_search"
    assert assembled[1]["function"]["name"] == "calc"


def test_empty_tool_calls():
    """No tool_calls in the stream."""
    chunks = [
        {"choices": [{"delta": {"content": "Hello"}}]},
        {"choices": [], "usage": {"total_tokens": 50}},
    ]
    assembled = accumulate_tool_calls(chunks)
    assert len(assembled) == 0


def test_text_and_tool_calls_mixed():
    """Text content and tool_calls mixed in the same stream."""
    chunks = [
        {"choices": [{"delta": {"content": "Let me search for that."}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_x", "function": {"name": "web_search", "arguments": '{"input":"test"}'}}
        ]}}]},
        {"choices": [], "usage": {"total_tokens": 80}},
    ]
    assembled = accumulate_tool_calls(chunks)
    assert len(assembled) == 1
    assert assembled[0]["function"]["name"] == "web_search"


def test_tool_call_name_split_across_chunks():
    """Tool call name may be split across chunks (rare but possible)."""
    chunks = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_n", "function": {"name": "web_", "arguments": ""}}
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "search", "arguments": '{"input":"q"}'}}
        ]}}]},
        {"choices": [], "usage": {"total_tokens": 60}},
    ]
    assembled = accumulate_tool_calls(chunks)
    assert len(assembled) == 1
    assert assembled[0]["function"]["name"] == "web_search"
    assert assembled[0]["function"]["arguments"] == '{"input":"q"}'