"""Tool Schema Auto-Generation — 自动生成 OpenAI Function Calling 格式的工具 schema。

从 Python 函数签名和 docstring 自动生成工具定义：
- 解析函数参数类型（str, int, float, bool, list, dict）
- 提取 docstring 描述
- 生成默认值和必填字段
- 支持类型提示和验证

使用示例：
    @auto_tool_schema
    async def web_search(query: str, max_results: int = 10) -> str:
        '''Search the web for information.
        
        Args:
            query: Search query string
            max_results: Maximum number of results to return
            
        Returns:
            Formatted search results
        '''
        ...
    
    # 自动生成:
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string"},
                    "max_results": {"type": "integer", "description": "Maximum number of results to return", "default": 10}
                },
                "required": ["query"]
            }
        }
    }
"""

import inspect
import re
from typing import Any, Callable, Dict, List, Optional, get_type_hints


# Python 类型到 JSON Schema 类型的映射
TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}

# Name-to-JSON-Schema mapping — used instead of eval() to convert
# type name strings (extracted from Optional[T] / Union[T, None])
# to their JSON Schema equivalents safely.
_NAME_TO_JSON = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
}


def _parse_docstring(docstring: str) -> Dict[str, Any]:
    """解析 docstring 提取函数描述和参数说明。
    
    支持格式：
        Description line 1
        Description line 2
        
        Args:
            param1: description
            param2: description
            
        Returns:
            return description
    """
    if not docstring:
        return {"description": "", "params": {}}
    
    lines = docstring.strip().split('\n')
    
    # 提取描述（第一个非空段落）
    desc_lines = []
    i = 0
    while i < len(lines) and lines[i].strip():
        desc_lines.append(lines[i].strip())
        i += 1
    
    description = ' '.join(desc_lines)
    
    # 提取参数说明
    params = {}
    in_args = False
    while i < len(lines):
        line = lines[i].strip()
        
        # 检测 Args: 段落
        if line.startswith('Args:') or line.startswith('Parameters:'):
            in_args = True
            i += 1
            continue
        
        # 检测 Returns: 或 Raises: 结束参数段
        if line.startswith('Returns:') or line.startswith('Raises:'):
            break
        
        # 解析参数行
        if in_args and ':' in line:
            match = re.match(r'^(\w+)\s*:\s*(.+)$', line)
            if match:
                param_name = match.group(1)
                param_desc = match.group(2).strip()
                params[param_name] = param_desc
        
        i += 1
    
    return {"description": description, "params": params}


def _get_json_type(python_type: Any) -> str:
    """将 Python 类型转换为 JSON Schema 类型。"""
    if python_type in TYPE_MAP:
        return TYPE_MAP[python_type]
    
    # 处理 Optional[T]
    type_str = str(python_type)
    if 'Optional' in type_str or 'Union' in type_str:
        # 尝试提取内部类型
        match = re.search(r'(?:Optional|Union)\[(\w+)', type_str)
        if match:
            inner_type = match.group(1)
            # Use safe name→json mapping instead of eval()
            if inner_type in _NAME_TO_JSON:
                return _NAME_TO_JSON[inner_type]
    
    # 默认返回 string
    return "string"


def auto_tool_schema(func: Callable) -> Dict[str, Any]:
    """装饰器/函数：从 Python 函数自动生成 OpenAI Function Calling schema。
    
    Args:
        func: 目标函数（可以是同步或异步函数）
        
    Returns:
        OpenAI Function Calling 格式的 schema 字典
    """
    # 获取函数签名
    sig = inspect.signature(func)
    hints = get_type_hints(func)
    
    # 解析 docstring
    docstring = inspect.getdoc(func) or ""
    parsed = _parse_docstring(docstring)
    
    # 构建参数 properties
    properties = {}
    required = []
    
    for param_name, param in sig.parameters.items():
        if param_name in ('self', 'cls', 'ctx'):
            continue
        
        # 获取类型
        param_type = hints.get(param_name, str)
        json_type = _get_json_type(param_type)
        
        # 构建属性定义
        prop = {
            "type": json_type,
            "description": parsed["params"].get(param_name, f"Parameter: {param_name}")
        }
        
        # 添加默认值
        if param.default is not inspect.Parameter.empty:
            prop["default"] = param.default
        else:
            # 没有默认值的参数是必填的
            required.append(param_name)
        
        properties[param_name] = prop
    
    # 构建完整的 schema
    schema = {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": parsed["description"] or f"Function: {func.__name__}",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required
            }
        }
    }
    
    return schema


def batch_auto_schemas(*funcs: Callable) -> List[Dict[str, Any]]:
    """批量生成多个函数的 tool schemas。
    
    Args:
        *funcs: 一个或多个函数
        
    Returns:
        schema 列表
    """
    return [auto_tool_schema(f) for f in funcs]


# 便捷装饰器
def tool_schema(func: Callable) -> Callable:
    """装饰器版本：在函数上附加 __schema__ 属性。
    
    使用：
        @tool_schema
        async def my_tool(arg: str) -> str:
            '''Description'''
            pass
        
        # 访问 schema: my_tool.__schema__
    """
    func.__schema__ = auto_tool_schema(func)
    return func
