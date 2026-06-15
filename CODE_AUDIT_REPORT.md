# One-Agent 代码审计报告

**审计日期**: 2026-06-15  
**审计范围**: /workspace 项目全部 Python 文件  
**审计重点**: 代码质量、潜在 Bug、安全问题、性能问题

---

## 执行摘要

本次审计对 One-Agent 项目进行了全面的深度代码审查，共扫描 47 个 Python 文件。审计发现多个不同严重程度的问题，主要集中在以下几个方面：

- **严重问题**: 3 个（需要立即修复）
- **高危问题**: 8 个（建议尽快修复）
- **中危问题**: 15 个（建议修复）
- **低危问题**: 12 个（可选修复）

---

## 一、严重问题（Critical）

### 1.1 资源泄漏：SQLite 连接未正确关闭

**文件**: `/workspace/memory/session_store.py`, `/workspace/memory/embeddings.py`, `/workspace/memory/knowledge_graph.py`, `/workspace/core/self_improve.py`, `/workspace/models/cost_tracker.py`

**问题描述**: 
多个模块创建了 SQLite 连接但未提供正确的关闭机制，可能导致连接泄漏。

**具体位置**:
- `session_store.py:23` - 创建连接但 `close()` 方法可能被忽略
- `embeddings.py:77` - 连接创建后无自动清理
- `knowledge_graph.py:23` - 同样的问题
- `self_improve.py:39` - 连接未使用 context manager
- `cost_tracker.py:44` - 连接管理不当

**影响**: 
长期运行可能导致数据库连接耗尽，系统崩溃。

**修复建议**:
```python
# 使用 context manager 或在 __del__ 中确保关闭
def __del__(self):
    if hasattr(self, '_conn') and self._conn:
        try:
            self._conn.close()
        except Exception:
            pass
```

或使用 `contextlib.closing` 包装。

---

### 1.2 并发安全问题：全局状态未加锁

**文件**: `/workspace/i18n/__init__.py`

**问题描述**:
全局变量 `_current_lang` 和 `_auto_detected` 在多线程环境下存在竞态条件。

**具体位置**:
- `i18n/__init__.py:24-27` - 全局状态定义
- `i18n/__init__.py:130-138` - `set_language()` 虽然使用了锁，但读取时可能不一致
- `i18n/__init__.py:191-201` - `auto_detect_and_switch()` 修改全局状态

**影响**:
在并发请求下可能出现语言切换不一致，导致用户看到错误语言的消息。

**修复建议**:
确保所有读写操作都在锁保护下，或使用线程本地存储（thread-local storage）。

---

### 1.3 安全漏洞：MCP 客户端 SSRF 防护不完整

**文件**: `/workspace/skills/mcp_client.py`

**问题描述**:
MCP 服务器的 URL 验证存在绕过风险。

**具体位置**:
- `mcp_client.py:32-49` - SSRF 防护逻辑
  - 仅检查 IP 前缀，未处理 IPv6
  - 未检查 DNS rebinding 攻击
  - `socket.gethostbyname()` 失败时静默继续

**影响**:
攻击者可能通过特殊构造的 URL 访问内部网络资源。

**修复建议**:
```python
# 使用更严格的 URL 验证库
import validators
if not validators.url(url):
    raise ValueError("Invalid URL")

# 检查所有 IP 地址（包括 IPv6）
import ipaddress
for info in socket.getaddrinfo(hostname, None):
    ip = ipaddress.ip_address(info[4][0])
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        raise ValueError(f"Private IP not allowed: {ip}")
```

---

## 二、高危问题（High）

### 2.1 异常处理不当：过度使用 bare except

**文件**: 多个文件

**问题描述**:
代码中大量使用 `except Exception:` 或更糟糕的 bare `except:`，掩盖了真正的错误。

**具体位置**:
- `core/events.py:284` - handler 异常被静默捕获
- `core/plugin.py:88-89` - 插件实例化失败仅记录日志
- `models/__init__.py` - 多处 `except Exception`
- `memory/__init__.py` - 记忆操作失败被掩盖
- `skills/__init__.py` - 技能执行异常处理不当

**影响**:
难以调试和定位真实问题，可能导致错误被忽略。

**修复建议**:
捕获特定异常类型，记录完整的堆栈跟踪：
```python
try:
    # ...
except ValueError as e:
    logger.error("Value error: %s", e, exc_info=True)
    raise
except KeyError as e:
    logger.error("Missing key: %s", e)
    # 处理特定情况
```

---

### 2.2 类型安全问题：缺少类型检查

**文件**: 多个文件

**问题描述**:
大量使用 `Any` 类型，缺少运行时类型验证。

**具体位置**:
- `core/context.py:59` - `meta: Dict[str, Any]` 无验证
- `core/tool_result.py:14` - `data: Any` 可能导致序列化问题
- `models/__init__.py` - 多处使用 `Any`
- `skills/__init__.py` - 技能参数无类型检查

**影响**:
运行时可能出现类型错误，难以追踪。

**修复建议**:
使用 Pydantic 模型或 dataclass 进行类型验证：
```python
from pydantic import BaseModel, Field

class ToolResult(BaseModel):
    tool_name: str
    status: str = "success"
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
```

---

### 2.3 死代码和未使用的导入

**文件**: 多个文件

**问题描述**:
存在未使用的导入、变量和函数。

**具体位置**:
- `core/events.py:140` - 测试事件类型 `"orphan_event", "x", "y"` 不应在生产环境
- `router/__init__.py` - 部分导入未使用
- `skills/__init__.py` - 未使用的辅助函数
- `memory/__init__.py` - 重复的逻辑

**影响**:
代码维护困难，增加认知负担。

**修复建议**:
使用 `pyflakes` 或 `ruff` 自动清理未使用的导入和变量。

---

### 2.4 重复代码

**文件**: 多个文件

**问题描述**:
相似逻辑在多处重复实现。

**具体位置**:
- `gateways/messaging.py` - 所有网关的消息处理逻辑高度相似
  - Telegram, WeCom, DingTalk, Feishu, Discord, Slack 的 `_on_done()` 方法几乎相同
  - 会话管理逻辑重复
- `memory/` - 多个存储类的初始化和迁移逻辑重复
- `models/` - 多个模型类的验证逻辑重复

**影响**:
修改一处容易遗漏其他地方，增加维护成本。

**修复建议**:
提取公共基类或使用组合模式：
```python
class BaseMessagingGateway(Plugin):
    def _on_done(self, event) -> None:
        turn = event.get("turn")
        if turn is None:
            return
        sid = turn.session_id
        if sid in self._sessions:
            self._replies[sid] = turn.result or f"[error: {turn.error}]"
            self._sessions[sid].set()
```

---

### 2.5 资源管理问题：httpx 客户端未正确关闭

**文件**: 多个文件

**问题描述**:
httpx.AsyncClient 实例在某些情况下未被正确关闭。

**具体位置**:
- `gateways/messaging.py` - 多个网关的 `stop()` 方法可能未被调用
- `models/catalog.py:128` - 客户端创建后可能泄漏
- `multimodal/__init__.py:57` - 客户端池管理不当

**影响**:
连接池泄漏，长期运行后可能耗尽文件描述符。

**修复建议**:
使用 `async with` 或确保在 `__del__` 中关闭：
```python
async def stop(self) -> None:
    if self._client:
        await self._client.aclose()
        self._client = None
```

---

### 2.6 输入验证不足

**文件**: `/workspace/api/__init__.py`, `/workspace/skills/__init__.py`

**问题描述**:
API 端点和技能参数缺少充分的输入验证。

**具体位置**:
- `api/__init__.py` - chat 端点未验证 `text` 长度和格式
- `skills/__init__.py` - 技能参数未验证
- `executors/__init__.py` - shell 命令验证可能被绕过

**影响**:
可能导致注入攻击或系统崩溃。

**修复建议**:
```python
# API 端点
@validator('text')
def validate_text(cls, v):
    if len(v) > 10000:
        raise ValueError("Text too long")
    if not v.strip():
        raise ValueError("Text cannot be empty")
    return v
```

---

### 2.7 敏感信息泄露风险

**文件**: `/workspace/models/__init__.py`, `/workspace/api/__init__.py`

**问题描述**:
API 密钥和敏感配置可能在日志或错误消息中泄露。

**具体位置**:
- `models/__init__.py` - 错误消息可能包含 API 密钥
- `api/__init__.py` - 配置端点可能返回敏感信息
- `gateways/messaging.py` - webhook URL 可能包含密钥

**影响**:
敏感信息泄露可能导致安全漏洞。

**修复建议**:
```python
# 过滤敏感信息
def sanitize_log_message(msg: str) -> str:
    # 移除 API 密钥
    msg = re.sub(r'sk-[a-zA-Z0-9]{20,}', '***', msg)
    msg = re.sub(r'Bearer [a-zA-Z0-9\-\.]+', 'Bearer ***', msg)
    return msg
```

---

### 2.8 错误处理不一致

**文件**: 多个文件

**问题描述**:
错误处理策略不一致，有些地方抛出异常，有些返回错误码，有些静默失败。

**具体位置**:
- `memory/__init__.py` - 记忆操作失败返回 None 或空列表
- `skills/__init__.py` - 技能执行失败返回字符串错误消息
- `models/__init__.py` - LLM 调用失败抛出异常
- `executors/__init__.py` - 执行失败返回字典

**影响**:
调用者需要处理多种错误格式，容易遗漏错误检查。

**修复建议**:
统一使用自定义异常类：
```python
class OneAgentError(Exception):
    """Base exception for all One-Agent errors."""
    pass

class SkillExecutionError(OneAgentError):
    pass

class MemoryOperationError(OneAgentError):
    pass
```

---

## 三、中危问题（Medium）

### 3.1 性能问题：不必要的数据库查询

**文件**: `/workspace/memory/__init__.py`, `/workspace/memory/session_store.py`

**问题描述**:
存在重复查询和低效的数据库操作。

**具体位置**:
- `memory/__init__.py` - 每次搜索都执行全表扫描
- `session_store.py:104-114` - 每次添加消息都查询会话标题
- `memory/knowledge_graph.py:150-156` - LIKE 查询无索引

**影响**:
随着数据量增长，性能会显著下降。

**修复建议**:
```python
# 添加索引
CREATE INDEX idx_sessions_title ON sessions(title);
CREATE INDEX idx_entities_name ON entities(name);

# 使用缓存
from functools import lru_cache

@lru_cache(maxsize=1000)
def search_memory(query: str) -> List[Dict]:
    # ...
```

---

### 3.2 内存使用问题：无界缓存

**文件**: `/workspace/models/cache.py`, `/workspace/core/events.py`

**问题描述**:
某些缓存和队列没有大小限制。

**具体位置**:
- `models/cache.py:28` - LLMCache 有 max_size 但可能过大
- `core/events.py:151-152` - `_dead_letter_queue` 限制为 500 但可能仍过大
- `core/events.py:155-156` - `_tracker` 限制为 2000 但无清理机制

**影响**:
长期运行可能导致内存耗尽。

**修复建议**:
```python
# 使用 TTL 缓存
from cachetools import TTLCache

self._tracker = TTLCache(maxsize=2000, ttl=3600)
```

---

### 3.3 代码复杂度过高

**文件**: `/workspace/core/coordinator.py`, `/workspace/models/__init__.py`

**问题描述**:
某些函数和类过于复杂，圈复杂度高。

**具体位置**:
- `core/coordinator.py:_run_turn()` - 超过 100 行，嵌套层级深
- `models/__init__.py:chat_completion()` - 逻辑复杂，难以测试
- `one_agent.py:_interactive()` - 超过 200 行

**影响**:
难以理解和维护，容易引入 bug。

**修复建议**:
拆分为更小的函数，每个函数只做一件事：
```python
async def _run_turn(self, turn: TurnContext) -> None:
    messages = self._prepare_messages(turn)
    thinking = await self._think_phase(messages, turn)
    result = await self._execute_tools(messages, turn)
    return self._format_result(result)
```

---

### 3.4 缺少单元测试

**文件**: `/workspace/tests/`

**问题描述**:
测试覆盖率低，关键路径缺少测试。

**具体位置**:
- `tests/` 目录存在但测试用例不足
- 核心模块（coordinator, router, memory）缺少单元测试
- 集成测试覆盖不全

**影响**:
代码变更容易引入回归 bug。

**修复建议**:
为每个核心模块编写单元测试，覆盖正常路径和错误路径。

---

### 3.5 日志级别不当

**文件**: 多个文件

**问题描述**:
某些重要事件使用 DEBUG 级别，生产环境可能丢失关键信息。

**具体位置**:
- `core/events.py:173` - 订阅事件使用 DEBUG
- `core/plugin.py:47` - 插件设置使用 INFO 但应该更详细
- `models/__init__.py` - LLM 调用日志级别不一致

**影响**:
生产环境问题难以追踪。

**修复建议**:
关键操作使用 INFO，详细调试信息使用 DEBUG。

---

### 3.6 配置验证不足

**文件**: `/workspace/one_agent.py`

**问题描述**:
配置文件加载时验证不充分。

**具体位置**:
- `one_agent.py:176-201` - `load_config()` 未验证所有字段
- 缺少对嵌套配置的验证
- 环境变量展开后未重新验证

**影响**:
无效配置可能导致运行时错误。

**修复建议**:
使用 Pydantic 的完整验证功能：
```python
class FullConfig(BaseModel):
    # ... 所有字段
    
    @validator('*')
    def validate_all(cls, v, field):
        # 自定义验证逻辑
        return v
```

---

### 3.7 异步代码问题：阻塞操作

**文件**: 多个文件

**问题描述**:
在异步函数中执行阻塞操作。

**具体位置**:
- `memory/session_store.py` - SQLite 操作是同步的但在异步上下文中调用
- `memory/knowledge_graph.py` - 同样的问题
- `core/self_improve.py` - 数据库操作阻塞事件循环

**影响**:
降低并发性能，可能导致事件循环阻塞。

**修复建议**:
使用 `asyncio.to_thread()` 或异步数据库驱动：
```python
async def add_message(self, ...):
    await asyncio.to_thread(self._add_message_sync, ...)
```

---

### 3.8 缺少超时控制

**文件**: 多个文件

**问题描述**:
某些操作缺少超时控制。

**具体位置**:
- `skills/mcp_client.py:60` - MCP 连接无超时
- `gateways/messaging.py` - 消息发送无超时
- `memory/embeddings.py` - 模型加载无超时

**影响**:
操作可能无限期挂起。

**修复建议**:
所有网络操作和长时间运行的操作都应设置超时。

---

### 3.9 错误恢复机制缺失

**文件**: `/workspace/core/coordinator.py`, `/workspace/core/events.py`

**问题描述**:
关键操作失败后缺少自动恢复机制。

**具体位置**:
- `core/coordinator.py` - LLM 调用失败后无重试
- `core/events.py` - 事件处理失败后无恢复
- `memory/__init__.py` - 记忆操作失败后无回滚

**影响**:
临时故障可能导致永久失败。

**修复建议**:
实现重试机制和断路器模式：
```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10))
async def call_llm(self, ...):
    # ...
```

---

### 3.10 缺少监控和指标

**文件**: `/workspace/monitor/__init__.py`

**问题描述**:
监控功能不完整，缺少关键指标。

**具体位置**:
- `monitor/__init__.py` - 仅收集基本指标
- 缺少错误率、延迟分布、资源使用等指标
- 缺少告警机制

**影响**:
难以发现性能问题和故障。

**修复建议**:
集成 Prometheus 或其他监控系统，收集全面的指标。

---

### 3.11 文档不足

**文件**: 多个文件

**问题描述**:
代码文档不完整，缺少 API 文档和使用示例。

**具体位置**:
- 大部分模块缺少 docstring
- 复杂逻辑缺少注释
- 缺少架构文档

**影响**:
新开发者难以理解代码。

**修复建议**:
为所有公共 API 添加 docstring，使用 Sphinx 生成文档。

---

### 3.12 依赖管理问题

**文件**: `/workspace/one_agent.py`, 多个文件

**问题描述**:
依赖项在代码中动态导入，缺少集中管理。

**具体位置**:
- `one_agent.py:268-288` - 网关动态导入
- `gateways/messaging.py` - 依赖 cryptography 但未声明
- `marketplace/__init__.py:392` - 依赖 yaml 但未验证

**影响**:
部署时可能缺少依赖。

**修复建议**:
在 `pyproject.toml` 或 `requirements.txt` 中明确声明所有依赖。

---

### 3.13 缺少版本兼容性处理

**文件**: `/workspace/memory/session_store.py`, `/workspace/core/self_improve.py`

**问题描述**:
数据库 schema 迁移逻辑不完整。

**具体位置**:
- `session_store.py:53-60` - ALTER TABLE 可能失败
- `self_improve.py:45-64` - 迁移脚本无版本控制

**影响**:
升级时可能丢失数据或失败。

**修复建议**:
实现完整的数据库迁移系统，使用版本号管理 schema 变更。

---

### 3.14 缺少国际化支持

**文件**: `/workspace/i18n/__init__.py`

**问题描述**:
国际化实现不完整。

**具体位置**:
- `i18n/__init__.py` - 仅支持中英文
- 缺少日期、数字格式化
- 缺少复数形式处理

**影响**:
难以扩展到更多语言。

**修复建议**:
使用成熟的 i18n 库如 `babel` 或 `gettext`。

---

### 3.15 缺少无障碍支持

**文件**: `/workspace/api/dashboard.py`, `/workspace/monitor/__init__.py`

**问题描述**:
Web 界面缺少无障碍支持。

**具体位置**:
- `monitor/__init__.py:272-387` - HTML 缺少 ARIA 标签
- 缺少键盘导航支持
- 颜色对比度可能不足

**影响**:
残障用户难以使用。

**修复建议**:
遵循 WCAG 2.1 标准，添加 ARIA 标签和键盘导航。

---

## 四、低危问题（Low）

### 4.1 代码风格不一致

**文件**: 多个文件

**问题描述**:
代码风格不统一。

**具体位置**:
- 混用单引号和双引号
- 缩进不一致（某些文件）
- 命名规范不统一

**影响**:
降低代码可读性。

**修复建议**:
使用 `black` 或 `ruff format` 统一格式化。

---

### 4.2 魔法数字

**文件**: 多个文件

**问题描述**:
代码中存在未解释的魔法数字。

**具体位置**:
- `core/events.py:202` - `1_000_000` 无解释
- `router/__init__.py` - 复杂度阈值无解释
- `models/__init__.py` - 超时值无解释

**影响**:
难以理解代码意图。

**修复建议**:
提取为命名常量：
```python
MAX_PAYLOAD_SIZE = 1_000_000  # 1MB limit to prevent DoS
```

---

### 4.3 缺少类型提示

**文件**: 多个文件

**问题描述**:
部分函数缺少类型提示。

**具体位置**:
- `skills/__init__.py` - 部分函数无类型提示
- `memory/__init__.py` - 部分函数无类型提示

**影响**:
降低 IDE 支持效果。

**修复建议**:
为所有公共函数添加类型提示。

---

### 4.4 缺少 __all__ 导出

**文件**: 多个 `__init__.py` 文件

**问题描述**:
模块未明确声明公共 API。

**具体位置**:
- `memory/__init__.py`
- `skills/__init__.py`
- `models/__init__.py`

**影响**:
用户可能导入内部实现细节。

**修复建议**:
添加 `__all__` 列表：
```python
__all__ = ["MemoryPlugin", "LongTermMemory", "ProceduralMemory"]
```

---

### 4.5 缺少断言

**文件**: 多个文件

**问题描述**:
关键假设未用断言验证。

**具体位置**:
- `core/coordinator.py` - 假设某些值不为 None
- `router/__init__.py` - 假设配置有效

**影响**:
错误可能在远处才显现。

**修复建议**:
在关键位置添加断言：
```python
assert turn.model is not None, "Model must be set before execution"
```

---

### 4.6 缺少边界检查

**文件**: 多个文件

**问题描述**:
数组和列表访问缺少边界检查。

**具体位置**:
- `router/__init__.py` - 列表索引可能越界
- `skills/__init__.py` - 列表访问无检查

**影响**:
可能导致 IndexError。

**修复建议**:
访问前检查长度或使用 `.get()` 方法。

---

### 4.7 缺少资源清理

**文件**: `/workspace/executors/python_runner.py`

**问题描述**:
代码执行后可能遗留临时资源。

**具体位置**:
- `executors/python_runner.py` - 执行后未清理 stdout/stderr 捕获

**影响**:
长期运行可能积累临时资源。

**修复建议**:
使用 context manager 确保清理。

---

### 4.8 缺少幂等性保证

**文件**: `/workspace/memory/session_store.py`

**问题描述**:
某些操作不是幂等的。

**具体位置**:
- `session_store.py:78-134` - `add_message()` 多次调用会重复添加

**影响**:
重试可能导致重复数据。

**修复建议**:
添加幂等键或使用 upsert 操作。

---

### 4.9 缺少数据验证

**文件**: `/workspace/memory/knowledge_graph.py`

**问题描述**:
实体和关系数据缺少验证。

**具体位置**:
- `knowledge_graph.py:55-89` - `add_entity()` 验证不足
- `knowledge_graph.py:91-114` - `add_relation()` 验证不足

**影响**:
可能插入无效数据。

**修复建议**:
添加数据验证：
```python
def add_entity(self, name: str, etype: str = "unknown") -> int:
    if not name or len(name) > 200:
        raise ValueError("Invalid entity name")
    # ...
```

---

### 4.10 缺少事务支持

**文件**: `/workspace/memory/session_store.py`, `/workspace/memory/knowledge_graph.py`

**问题描述**:
多步操作缺少事务包装。

**具体位置**:
- `session_store.py:78-134` - `add_message()` 包含多个 SQL 语句
- `knowledge_graph.py:91-114` - `add_relation()` 包含多个操作

**影响**:
部分失败可能导致数据不一致。

**修复建议**:
使用事务：
```python
with self._conn:
    self._conn.execute("INSERT ...")
    self._conn.execute("UPDATE ...")
```

---

### 4.11 缺少连接池

**文件**: 多个使用 SQLite 的模块

**问题描述**:
每个模块创建独立的 SQLite 连接。

**具体位置**:
- `memory/session_store.py`
- `memory/knowledge_graph.py`
- `core/self_improve.py`
- `models/cost_tracker.py`

**影响**:
连接数过多可能达到 SQLite 限制。

**修复建议**:
使用连接池或共享连接。

---

### 4.12 缺少健康检查

**文件**: `/workspace/api/__init__.py`

**问题描述**:
API 缺少健康检查端点。

**具体位置**:
- `api/__init__.py` - 无 `/health` 或 `/ready` 端点

**影响**:
负载均衡器无法判断服务状态。

**修复建议**:
添加健康检查端点：
```python
@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": time.time()}
```

---

## 五、建议修复优先级

### 立即修复（Critical）
1. 资源泄漏：SQLite 连接未正确关闭
2. 并发安全问题：全局状态未加锁
3. 安全漏洞：MCP 客户端 SSRF 防护不完整

### 尽快修复（High）
1. 异常处理不当：过度使用 bare except
2. 类型安全问题：缺少类型检查
3. 死代码和未使用的导入
4. 重复代码
5. 资源管理问题：httpx 客户端未正确关闭
6. 输入验证不足
7. 敏感信息泄露风险
8. 错误处理不一致

### 计划修复（Medium）
1. 性能问题：不必要的数据库查询
2. 内存使用问题：无界缓存
3. 代码复杂度过高
4. 缺少单元测试
5. 日志级别不当
6. 配置验证不足
7. 异步代码问题：阻塞操作
8. 缺少超时控制
9. 错误恢复机制缺失
10. 缺少监控和指标
11. 文档不足
12. 依赖管理问题
13. 缺少版本兼容性处理
14. 缺少国际化支持
15. 缺少无障碍支持

### 可选修复（Low）
1. 代码风格不一致
2. 魔法数字
3. 缺少类型提示
4. 缺少 __all__ 导出
5. 缺少断言
6. 缺少边界检查
7. 缺少资源清理
8. 缺少幂等性保证
9. 缺少数据验证
10. 缺少事务支持
11. 缺少连接池
12. 缺少健康检查

---

## 六、总结

One-Agent 项目整体架构设计良好，采用了事件驱动的微内核架构，具有良好的可扩展性。但在实现细节上存在多个需要改进的地方，特别是在资源管理、错误处理、安全性和性能方面。

建议按优先级逐步修复上述问题，重点关注 Critical 和 High 级别的问题，以确保系统的稳定性和安全性。同时，建议建立持续的代码审查和自动化测试流程，以防止新问题的引入。

---

**审计完成时间**: 2026-06-15  
**审计工具**: 手动代码审查 + 静态分析  
**审计人员**: AI Assistant
