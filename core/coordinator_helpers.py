"""Pure helper functions extracted from Coordinator.

These functions don't depend on Coordinator's mutable state and can
be tested in isolation. They were extracted as Phase 1 of the
Coordinator refactoring (P0-1).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# 已知工具名集合 — 用于校验解析出的标签名是否合法
XML_TOOL_NAMES = frozenset({
    "web_search", "python_execute", "calc", "send_message",
    "system_run", "settings", "model_manage", "now", "email",
    "calendar", "database", "mcp", "openapi", "workflow",
    "chart", "branch", "branch_switch", "branch_list",
})


def append_to_content(content: Any, suffix: str) -> Any:
    """Append text to a message content field, compatible with both
    str and list (vision/multimodal) content formats.

    修复：之前直接 content + "\n\n" + hint 假设 content 是 str,
    但 OpenAI/Anthropic 多模态格式 content 是 list (如
    [{"type":"text","text":"..."},{"type":"image_url",...}]),
    str + list 抛 TypeError。
    """
    if isinstance(content, str):
        return content + "\n\n" + suffix
    if isinstance(content, list):
        # 多模态：追加一个 text 块
        return content + [{"type": "text", "text": suffix}]
    # content 为 None 或其他类型：直接返回 suffix 作为字符串
    return suffix


def prepend_to_content(content: Any, prefix: str) -> Any:
    """Prepend text to a message content field (compatible with str/list)."""
    if isinstance(content, str):
        return prefix + "\n\n" + content
    if isinstance(content, list):
        # 多模态：在开头插入一个 text 块
        return [{"type": "text", "text": prefix}] + content
    return prefix


def sanitize_model_output(text: str) -> str:
    """Remove XML tool-call tags that weak models may emit as text.

    Some models (especially flash/lite variants) output tool-call XML
    like <invoke name="web_search">...</invoke> or <tool_call ...>...
    directly in their text response instead of using the proper API.
    This strips those tags so users never see raw XML.
    """
    import re
    # Remove <invoke ...>...</invoke> blocks
    text = re.sub(
        r'<invoke\s+name="[^"]*">.*?</invoke>',
        '',
        text,
        flags=re.DOTALL,
    )
    # Remove <parameter ...>...</parameter> blocks
    text = re.sub(
        r'<parameter\s+name="[^"]*">.*?</parameter>',
        '',
        text,
        flags=re.DOTALL,
    )
    # Remove standalone <invoke ...> tags (unclosed)
    text = re.sub(r'<invoke\s+name="[^"]*"[^>]*/?\s*>', '', text)
    # Remove <tool_call ...>...</tool_call > blocks
    text = re.sub(
        r'<tool_call[^>]*>.*?</tool_call\s*>',
        '',
        text,
        flags=re.DOTALL,
    )
    # Remove <function_call ...>...</function_call> blocks
    text = re.sub(
        r'<function_call[^>]*>.*?</function_call\s*>',
        '',
        text,
        flags=re.DOTALL,
    )
    # Remove self-closing tool tags like <web_search query="..."/>
    # or <system_run command="..."/> that weak models emit when they
    # can't use function calling. These may have been parsed & executed
    # by _parse_xml_tool_tags, but the raw tags must not leak to users.
    text = strip_executed_xml_tags(text)
    # Clean up excessive blank lines left behind
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def parse_xml_tool_tags(text: str) -> List[Dict[str, Any]]:
    """从 LLM 输出文本中解析自闭合 XML 工具标签，转为 tool_calls 结构。

    当模型不支持 OpenAI function calling（如 sensenova flash-lite）时，
    LLM 可能输出 <web_search query="..."/> 这类标签来表达工具调用意图。
    本方法把这些标签解析成标准 tool_calls 结构，让 _execute_tool_calls 能执行。

    支持的格式（自闭合，属性即参数）：
        <web_search query="dmxapi API 文档" />
        <system_run command="curl -s https://api.dmxapi.cn/v1/models" />
        <calc expr="1+1" />

    也支持成对标签：
        <web_search query="..."></web_search>

    返回 [] 表示没有可解析的工具标签。
    """
    import re as _re
    import json as _json

    if not text or "<" not in text:
        return []

    calls: List[Dict[str, Any]] = []
    # 匹配 <tool_name attr1="v1" attr2='v2' />  或  <tool_name ...></tool_name>
    # 限制 tool_name 只能是已知工具名，避免误解析普通 XML/HTML
    pattern = _re.compile(
        r'<(?P<name>[a-z_]+)(?P<attrs>(?:\s+[a-zA-Z_]\w*\s*=\s*(?:"[^"]*"|\'[^\']*\'))+)\s*/?>',
        _re.IGNORECASE,
    )
    attr_pattern = _re.compile(
        r'(?P<key>[a-zA-Z_]\w*)\s*=\s*(?P<val>"(?P<dval>[^"]*)"|\'(?P<sval>[^\']*)\')',
    )

    for m in pattern.finditer(text):
        name = m.group("name").lower()
        if name not in XML_TOOL_NAMES:
            continue
        attrs_str = m.group("attrs")
        args: Dict[str, Any] = {}
        for am in attr_pattern.finditer(attrs_str):
            key = am.group("key")
            # 优先双引号值，否则单引号值
            val = am.group("dval") if am.group("dval") is not None else am.group("sval")
            args[key] = val

        if not args:
            continue

        # 参数名映射：LLM 在 XML 标签里用的参数名可能和技能 schema 定义的不一致。
        # 例如 web_search 的 schema 定义 required=["input"]，但 LLM 输出
        # <web_search query="..."/> 用 query。这里做统一映射。
        _XML_PARAM_MAP = {
            "web_search": {"query": "input", "q": "input"},
            "calc": {"expr": "input", "expression": "input"},
        }
        if name in _XML_PARAM_MAP:
            for xml_key, mapped_key in _XML_PARAM_MAP[name].items():
                if xml_key in args and mapped_key not in args:
                    args[mapped_key] = args.pop(xml_key)

        # 兼容 _execute_tool_calls 期望的字段格式
        calls.append({
            "id": f"xml_{len(calls)}_{name}",
            "name": name,
            "args": args,
        })

    return calls


def parse_markdown_tool_calls(text: str) -> List[Dict[str, Any]]:
    """从 LLM 输出文本中解析 markdown 代码块格式的工具调用。

    当 LLM 不使用 function_call 格式，而是用文字描述工具调用时，
    常见格式是：

    ```bash
    web_search("Agnes AI API endpoint")
    ```

    或：

    ```python
    system_run("curl -s https://example.com/v1/models")
    ```

    本方法解析 ```` ```lang \\n func_name("args") \\n``` ```` 格式，
    提取函数名和参数，转为 tool_calls 结构。

    返回 [] 表示没有可解析的工具调用。
    """
    import re as _re
    import json as _json
    import codecs as _codecs

    if not text:
        return []

    calls: List[Dict[str, Any]] = []
    # 匹配 ```lang\nfunc_name(args)\n``` 格式
    # 使用平衡括号匹配支持嵌套括号（如 func(foo(1)) 或 func({"k":"v"})）
    block_pattern = _re.compile(
        r'```(?:\w+)?\s*\n\s*(?P<call>[a-z_]\w*\s*\([^()]*?(?:\([^()]*\)[^()]*)*\))\s*\n\s*```',
        _re.IGNORECASE | _re.MULTILINE | _re.DOTALL,
    )
    # 裸调用格式：func_name("...")（只匹配代码块内或独立的工具调用行）
    bare_pattern = _re.compile(
        r'(?m)^\s*(?P<call>[a-z_]\w*\s*\([^()]*?(?:\([^()]*\)[^()]*)*\))\s*$',
        _re.IGNORECASE,
    )

    # 参数名映射：和 XML 解析器保持一致
    _MD_PARAM_MAP = {
        "web_search": {"query": "input", "q": "input", "search": "input"},
        "calc": {"expr": "input", "expression": "input"},
        "system_run": {"command": "command", "cmd": "command"},
    }

    seen_calls: set = set()  # 去重

    for pat in (block_pattern, bare_pattern):
        for m in pat.finditer(text):
            call_str = m.group("call").strip()
            # 解析 func_name(args) 格式（支持嵌套括号）
            parts = _re.match(r'(?P<name>[a-z_]\w*)\s*\((?P<args>.*)\)', call_str, _re.IGNORECASE | _re.DOTALL)
            if not parts:
                continue
            name = parts.group("name").lower()
            if name not in XML_TOOL_NAMES:
                continue
            args_str = parts.group("args").strip()
            if not args_str:
                continue

            # 去重：同名同参数的调用只保留一个
            dedup_key = f"{name}:{args_str[:200]}"
            if dedup_key in seen_calls:
                continue
            seen_calls.add(dedup_key)

            # 解析参数
            args: Dict[str, Any] = {}

            # 先尝试关键字参数解析：key="value" 或 key='value'
            kw_pattern = _re.compile(
                r'(?P<key>[a-zA-Z_]\w*)\s*=\s*(?:"(?P<dval>(?:[^"\\]|\\.)*)"|\'(?P<sval>(?:[^\'\\]|\\.)*)\')',
            )
            kw_matches = list(kw_pattern.finditer(args_str))

            def _unescape(s: str) -> str:
                """反转义 \\\" \\\\ \\n 等转义序列"""
                try:
                    # codecs.decode 处理 \n \t \x.. \u....
                    # 但不处理 \" → "（json 风格），需要单独处理
                    s = s.replace('\\"', '"').replace("\\'", "'")
                    return _codecs.decode(s, 'unicode_escape')
                except Exception:
                    return s

            if kw_matches:
                for km in kw_matches:
                    key = km.group("key")
                    if km.group("dval") is not None:
                        val = _unescape(km.group("dval"))
                    elif km.group("sval") is not None:
                        val = _unescape(km.group("sval"))
                    else:
                        val = km.group("nval") if km.group("nval") else ""
                    args[key] = val
            else:
                # 单个位置参数：提取引号内的字符串（支持转义引号）
                str_match = _re.match(r'^"((?:[^"\\]|\\.)*)"$', args_str, _re.DOTALL)
                if not str_match:
                    str_match = _re.match(r"^'((?:[^'\\]|\\.)*)'$", args_str, _re.DOTALL)
                if str_match:
                    raw_val = _unescape(str_match.group(1))
                    if name in _MD_PARAM_MAP:
                        first_key = next(iter(_MD_PARAM_MAP[name].values()))
                        args[first_key] = raw_val
                    else:
                        args["input"] = raw_val
                else:
                    # 尝试 JSON 解析
                    try:
                        parsed = _json.loads(args_str)
                        if isinstance(parsed, dict):
                            args = parsed
                        elif isinstance(parsed, str):
                            args["input"] = parsed
                    except (_json.JSONDecodeError, ValueError):
                        # 裸字符串
                        args["input"] = args_str

            # 参数名映射
            if name in _MD_PARAM_MAP:
                for md_key, mapped_key in _MD_PARAM_MAP[name].items():
                    if md_key in args and mapped_key not in args:
                        args[mapped_key] = args.pop(md_key)

            if not args:
                continue

            calls.append({
                "id": f"md_{len(calls)}_{name}",
                "name": name,
                "args": args,
            })

    return calls


def strip_executed_xml_tags(text: str) -> str:
    """从输出文本中移除已解析执行过的 XML 工具标签，避免泄漏给用户。

    与 _sanitize_model_output 不同，本方法只移除已知工具名的自闭合标签，
    保留普通文本内容。在 _parse_xml_tool_tags 成功解析后调用。
    """
    import re as _re
    if not text or "<" not in text:
        return text
    # 构建正则：<(?:web_search|system_run|calc|...)\s+.../>
    names_alt = "|".join(_re.escape(n) for n in XML_TOOL_NAMES)
    # 移除自闭合标签 <tool_name .../>
    text = _re.sub(
        rf'<(?:{names_alt})(?:\s+[a-zA-Z_]\w*\s*=\s*(?:"[^"]*"|\'[^\']*\'))+\s*/?>',
        '',
        text,
        flags=_re.IGNORECASE,
    )
    # 移除成对空标签 <tool_name ...></tool_name>
    text = _re.sub(
        rf'<(?:{names_alt})(?:\s[^>]*)?>\s*</(?:{names_alt})>',
        '',
        text,
        flags=_re.IGNORECASE,
    )
    # 清理多余空行
    text = _re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def needs_clarification_check(text: str) -> bool:
    """Heuristic: should we even bother asking the LLM if input is ambiguous?

    Short, specific questions (e.g. "现在几点", "1+1=") don't need the
    clarification check even at complex tier — it would just waste a call.
    """
    # Very short inputs are usually clear commands or questions
    if len(text) < 15:
        return False
    # Inputs with code blocks, URLs, or file paths are usually concrete tasks
    if any(marker in text for marker in ("```", "http", "/workspace", ".py", ".js")):
        return False
    return True


def needs_web_search(text: str) -> bool:
    """Heuristic check whether the user's request requires web search.

    Used as a fallback when the model doesn't support tool calling —
    so the agent can still look up real-time info.
    """
    import re
    t = text.lower()
    search_patterns = [
        r"搜索|搜|查找|查一下|查一查|查新|最新|最近|新闻|资讯|头条|热门|热搜|实时|今天|昨天|近日|近期",
        r"web search|search for|look up|find out|what's new|what is new|latest|recent news|current events|breaking",
        r"价格|股价|行情|比分|比赛结果|天气|汇率|价格表|排行榜",
        r"how much|how many|price of|weather|score|result",
    ]
    for pat in search_patterns:
        if re.search(pat, t):
            return True
    if re.search(r"今年|本月|本周|今天|现在|目前|当前|2025|2026", t) and len(t) > 10:
        return True
    return False


def parse_planned_tools(
    plan_text: str, available_tool_names: set,
) -> List[str]:
    """Gap 6：从工具链规划文本中提取预期的工具调用顺序。

    匹配规则：规划里出现的、且当前确实可用的工具名，按首次出现顺序返回。
    没有规划或匹配不到时返回空列表（不约束）。
    """
    if not plan_text or not available_tool_names:
        return []
    ordered: List[str] = []
    seen: set = set()
    # 工具名通常是 word-boundary 的标识符（如 web_search、calc、system_run）
    for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", plan_text):
        if name in available_tool_names and name not in seen:
            ordered.append(name)
            seen.add(name)
    return ordered


def detect_output_format(answer: str) -> str:
    """Gap 修复：检测回复类型，返回格式提示。

    之前 verify_and_polish 的 prompt 只说"优化结构"，LLM 可能把代码块
    优化成纯文本描述、把表格优化成段落。现在先检测格式类型，注入提示保格式。
    """
    if "```" in answer and ("def " in answer or "class " in answer or "import " in answer):
        return "代码块格式（保留 ``` 代码块）"
    if "```" in answer:
        return "代码块格式"
    if "|" in answer and "---" in answer:
        return "表格格式"
    if re.search(r"^\d+\.\s", answer, re.MULTILINE) or re.search(r"^-\s", answer, re.MULTILINE):
        if len(answer) > 500:
            return "列表+要点总结格式"
        return "列表格式"
    if "http" in answer and len(answer) > 300:
        return "保留链接和引用"
    return ""
