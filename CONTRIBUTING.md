# 贡献指南

感谢你对 One-Agent 项目的关注！本文档描述了参与开发所需的流程与规范。

---

## 开发环境搭建

### 前置要求

- Python >= 3.10
- pip 或 uv

### 步骤

```bash
# 1. 克隆仓库
git clone https://github.com/your-org/one-agent.git
cd one-agent

# 2. 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# 3. 安装开发依赖
pip install -e ".[full]"
pip install pytest pytest-asyncio pytest-cov pytest-timeout ruff

# 4. 验证安装
python -c "import one_agent; print(one_agent.__version__)"
```

---

## 代码规范

### Lint 与格式化

项目使用 [ruff](https://docs.astral.sh/ruff/) 进行代码检查与格式化：

```bash
# 检查
ruff check .

# 自动修复
ruff check . --fix

# 格式化
ruff format .
```

**规则配置**见 `pyproject.toml` 中的 `[tool.ruff]` 段：

| 规则集 | 说明 |
|--------|------|
| E/W | pycodestyle 错误与警告 |
| F | pyflakes（未使用导入/变量等） |
| I | isort 导入排序 |
| UP | pyupgrade 语法现代化 |
| B | flake8-bugbear 常见陷阱 |

**行宽**：100 字符
**引号风格**：双引号
**目标版本**：Python 3.10

### 类型注解

- 使用 `from __future__ import annotations` 启用延迟注解求值
- 公共 API 函数应添加类型注解
- Pydantic 模型字段使用 `Optional[X]` 和 `Dict[str, Any]`（保持与 pydantic v2 兼容）

### 注释语言

- 模块/函数/类 docstring：英文优先，复杂业务逻辑可用中文
- 行内注释：与上下文语言一致
- 用户可见消息（日志/异常/API 响应）：使用 i18n `_()` 函数包装

---

## 测试

### 运行测试

```bash
# 全部测试
python -m pytest tests/ -v

# 带覆盖率
python -m pytest tests/ --cov=. --cov-report=term-missing

# 单个测试文件
python -m pytest tests/unit_tests.py -v

# 超时设置
python -m pytest tests/ --timeout=120
```

### 测试规范

- 测试文件放在 `tests/` 目录，命名 `test_*.py` 或 `*_test.py`
- 使用 pytest 原生 `assert`，不依赖第三方断言库
- 异步测试使用 `pytest-asyncio`（`asyncio_mode = "auto"`）
- 每个测试应独立，不依赖执行顺序
- 使用 stub/mock 隔离外部依赖（LLM/数据库/网络）

### 测试覆盖范围

| 测试文件 | 覆盖内容 |
|----------|----------|
| `tests/unit_tests.py` | 核心单元测试 |
| `tests/test_new_features.py` | 新功能测试 |
| `tests/test_coordinator_paths.py` | Coordinator 核心路径 |
| `tests/test_executor_events.py` | 执行器与事件总线 |
| `tests/e2e_test.py` | 端到端测试 |
| `tests/smoke.py` | 冒烟测试 |

---

## 提交规范

### Commit Message 格式

```
<type>: <description>

[optional body]
```

**type** 可选值：

| type | 说明 |
|------|------|
| feat | 新功能 |
| fix | Bug 修复 |
| refactor | 重构（不改变外部行为） |
| docs | 文档变更 |
| test | 测试相关 |
| chore | 构建/工具/依赖变更 |
| ci | CI 配置变更 |

**示例**：
```
feat: 添加 BaseExecutor 抽象基类统一执行器接口
fix: 修复 api/__init__.py 中 sqlite3 未导入导致健康检查失败
refactor: AlertManager 改为继承 Plugin 由 PluginManager 统一管理
```

### 提交前检查清单

- [ ] `ruff check .` 通过（0 errors）
- [ ] `python -m pytest tests/ -v` 通过
- [ ] 新功能有对应测试
- [ ] CHANGELOG.md 已更新（如适用）
- [ ] 无敏感信息（API key/token/密码）

---

## 架构指南

### 插件系统

所有子系统（执行器/记忆/技能/告警/监控）均继承 `Plugin` 基类，由 `PluginManager` 统一管理生命周期：

```python
class MyPlugin(Plugin):
    name = "my_plugin"
    depends_on = ["memory"]  # 可选：声明依赖

    async def setup(self, ctx) -> None:
        """初始化插件。"""
        ...

    async def start(self) -> None:
        """启动插件。"""
        ...

    async def stop(self) -> None:
        """停止插件，释放资源。"""
        ...
```

### 执行器接口

所有执行器继承 `BaseExecutor`，统一使用 `execute()` 入口和 `ExecutorResult` 返回类型：

```python
class MyExecutor(BaseExecutor):
    name = "my_executor"

    async def execute(self, command: str, **kwargs) -> ExecutorResult:
        """统一入口方法。"""
        ...
        return _to_executor_result({"exit_code": 0, "stdout": "...", "success": True})
```

### 事件总线

事件通过 `EventBus.publish()` 发布，支持两种 payload 格式：

```python
# 嵌套格式（推荐）
bus.publish({
    "type": "my_event",
    "payload": {"key": "value"},
    "source": "my_plugin",
})

# 扁平格式（兼容）
bus.publish({
    "type": "my_event",
    "key": "value",  # 自动合并到 payload
})
```

### 资源清理

- 使用显式 `close()` 方法释放资源（数据库连接/文件句柄/网络连接）
- `__del__` 仅作兜底，不依赖其执行时机
- 推荐使用 `async with` 或 `contextlib.closing`

---

## Pull Request 流程

1. Fork 仓库并创建特性分支：`git checkout -b feat/my-feature`
2. 编写代码并确保通过所有检查
3. 提交 commit（遵循提交规范）
4. 推送分支并创建 Pull Request
5. 等待 CI 检查通过
6. 响应 code review 反馈
7. 合并后删除特性分支

### PR 标题

与 commit message 的 `<type>: <description>` 格式一致。

### PR 描述

```
## 变更说明
<!-- 描述本次 PR 做了什么 -->

## 变更类型
- [ ] 新功能
- [ ] Bug 修复
- [ ] 重构
- [ ] 文档
- [ ] 测试
- [ ] CI/构建

## 检查清单
- [ ] ruff check 通过
- [ ] 测试通过
- [ ] CHANGELOG 已更新（如适用）
```

---

## 问题反馈

- **Bug 报告**：使用 GitHub Issues，附上复现步骤和环境信息
- **功能建议**：先在 Discussions 中讨论，达成共识后再创建 Issue
- **安全问题**：请勿公开报告，发送邮件至 security@example.com
