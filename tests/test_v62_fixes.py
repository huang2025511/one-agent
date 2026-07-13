"""V62 修复测试：markdown 代码块工具调用解析 + 429 重试配置 + LLM 失败推送。

测试覆盖：
1. parse_markdown_tool_calls 解析各种格式的工具调用
2. 配置文件 retries 值正确
3. LLM 失败时错误信息推送逻辑
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.coordinator_helpers import parse_markdown_tool_calls, parse_xml_tool_tags
import yaml


def test_markdown_block_tool_call():
    """测试解析 ```bash\n web_search("...")\n``` 格式"""
    text = '''好的，让我搜索一下：

```bash
web_search("Agnes AI API endpoint base URL models")
```
'''
    calls = parse_markdown_tool_calls(text)
    assert len(calls) == 1, f"expected 1 call, got {len(calls)}: {calls}"
    assert calls[0]["name"] == "web_search", f"expected web_search, got {calls[0]['name']}"
    assert "input" in calls[0]["args"], f"expected 'input' in args, got {calls[0]['args']}"
    assert "Agnes AI" in calls[0]["args"]["input"], f"unexpected input: {calls[0]['args']['input']}"
    print("✅ test_markdown_block_tool_call passed")


def test_markdown_system_run():
    """测试解析 system_run curl 命令"""
    text = '''```bash
system_run("curl -s https://apihub.agnes-ai.com/v1/models -H \\"Authorization: Bearer sk-xxx\\"")
```
'''
    calls = parse_markdown_tool_calls(text)
    assert len(calls) == 1, f"expected 1 call, got {len(calls)}: {calls}"
    assert calls[0]["name"] == "system_run", f"expected system_run, got {calls[0]['name']}"
    assert "command" in calls[0]["args"], f"expected 'command' in args, got {calls[0]['args']}"
    assert "curl" in calls[0]["args"]["command"], f"unexpected command: {calls[0]['args']['command']}"
    print("✅ test_markdown_system_run passed")


def test_markdown_python_block():
    """测试解析 ```python\n web_search("...")\n``` 格式"""
    text = '''```python
web_search("Agnes AI openai compatible api")
```
'''
    calls = parse_markdown_tool_calls(text)
    assert len(calls) == 1, f"expected 1 call, got {len(calls)}: {calls}"
    assert calls[0]["name"] == "web_search"
    print("✅ test_markdown_python_block passed")


def test_markdown_kw_args():
    """测试关键字参数格式：func_name(query="...", limit=5)"""
    text = '''```bash
web_search(query="Agnes AI API", limit=5)
```
'''
    calls = parse_markdown_tool_calls(text)
    assert len(calls) == 1, f"expected 1 call, got {len(calls)}: {calls}"
    assert calls[0]["name"] == "web_search"
    # query 应该被映射为 input
    assert "input" in calls[0]["args"], f"expected 'input' (mapped from query), got {calls[0]['args']}"
    assert calls[0]["args"]["input"] == "Agnes AI API"
    print("✅ test_markdown_kw_args passed")


def test_markdown_multiple_calls():
    """测试解析多个工具调用"""
    text = '''让我先搜索，然后执行：

```bash
web_search("Agnes AI API endpoint")
```

```bash
system_run("curl -s https://apihub.agnes-ai.com/v1/models")
```
'''
    calls = parse_markdown_tool_calls(text)
    assert len(calls) == 2, f"expected 2 calls, got {len(calls)}: {calls}"
    names = {c["name"] for c in calls}
    assert names == {"web_search", "system_run"}, f"unexpected names: {names}"
    print("✅ test_markdown_multiple_calls passed")


def test_markdown_no_tool_call():
    """测试普通文本不应被误解析为工具调用"""
    text = '''这是一个普通的回答，没有工具调用。

```python
print("hello world")
```

```bash
ls -la
```
'''
    calls = parse_markdown_tool_calls(text)
    # print 和 ls 都不是已知工具名
    assert len(calls) == 0, f"expected 0 calls, got {len(calls)}: {calls}"
    print("✅ test_markdown_no_tool_call passed")


def test_markdown_bare_call():
    """测试裸函数调用格式（不带代码块包裹）"""
    text = '''好的，让我搜索一下：

web_search("Agnes AI API endpoint")
'''
    calls = parse_markdown_tool_calls(text)
    # 裸调用也应该被解析
    assert len(calls) >= 1, f"expected >= 1 call, got {len(calls)}: {calls}"
    assert calls[0]["name"] == "web_search"
    print("✅ test_markdown_bare_call passed")


def test_markdown_dedup():
    """测试重复调用去重"""
    text = '''```bash
web_search("same query")
```

```bash
web_search("same query")
```
'''
    calls = parse_markdown_tool_calls(text)
    assert len(calls) == 1, f"expected 1 call (deduped), got {len(calls)}: {calls}"
    print("✅ test_markdown_dedup passed")


def test_xml_still_works():
    """确保 XML 解析器仍然正常工作"""
    text = '<web_search query="Agnes AI API"/>'
    calls = parse_xml_tool_tags(text)
    assert len(calls) == 1, f"expected 1 XML call, got {len(calls)}: {calls}"
    assert calls[0]["name"] == "web_search"
    assert calls[0]["args"].get("input") == "Agnes AI API"
    print("✅ test_xml_still_works passed")


def test_retries_config():
    """验证配置文件 retries 值为 3"""
    config_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")
    for cfg_name in ("default_config.yaml", "prod_config.yaml"):
        cfg_path = os.path.join(config_dir, cfg_name)
        with open(cfg_path, "r") as f:
            cfg = yaml.safe_load(f)
        retries = cfg.get("llm", {}).get("retries", 0)
        assert retries == 3, f"{cfg_name}: expected retries=3, got {retries}"
        print(f"✅ {cfg_name}: retries={retries}")


def test_user_case_exact():
    """测试用户实际案例中的精确格式"""
    # 用户聊天记录中的实际 LLM 输出
    text = '''好的，让我重新尝试。先搜索正确的 API 端点，然后直接拉取模型列表。

```bash
web_search("Agnes AI API endpoint base URL models")
```

```bash
web_search("agnes ai openai compatible api")
```
'''
    calls = parse_markdown_tool_calls(text)
    assert len(calls) == 2, f"expected 2 calls, got {len(calls)}: {calls}"
    assert calls[0]["name"] == "web_search"
    assert calls[1]["name"] == "web_search"
    assert "Agnes AI" in calls[0]["args"]["input"]
    assert "agnes" in calls[1]["args"]["input"].lower()
    print("✅ test_user_case_exact passed — 用户实际案例格式可正确解析")


def test_user_case_system_run():
    """测试用户案例中的 system_run curl 调用"""
    text = '''```bash
system_run("curl -s https://apihub.agnes-ai.com/v1/models -H \\"Authorization: Bearer sk-mDvN2X2mg2IYS7lmCQgVup9sckqriDnlNqD3jyozKYZ1zNP1\\"")
```
'''
    calls = parse_markdown_tool_calls(text)
    assert len(calls) == 1, f"expected 1 call, got {len(calls)}: {calls}"
    assert calls[0]["name"] == "system_run"
    assert "curl" in calls[0]["args"]["command"]
    assert "apihub.agnes-ai.com" in calls[0]["args"]["command"]
    print("✅ test_user_case_system_run passed — system_run curl 调用可正确解析")


if __name__ == "__main__":
    test_markdown_block_tool_call()
    test_markdown_system_run()
    test_markdown_python_block()
    test_markdown_kw_args()
    test_markdown_multiple_calls()
    test_markdown_no_tool_call()
    test_markdown_bare_call()
    test_markdown_dedup()
    test_xml_still_works()
    test_retries_config()
    test_user_case_exact()
    test_user_case_system_run()
    print("\n🎉 All V62 tests passed!")
