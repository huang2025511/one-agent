# Changelog

All notable changes to One-Agent are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] — 2026-06-17

### Added
- **BaseExecutor 抽象基类**：统一所有执行器（Shell/Docker/Browser/Python/System）的接口，定义 `execute()` 为标准入口方法
- **ExecutorResult 统一返回类型**：解决 `blocked/ok/success/returncode/exit_code/stdout/output` 字段不统一问题，提供 canonical 属性 + legacy 别名
- **CI 矩阵测试**：GitHub Actions 支持 Python 3.10/3.11/3.12 矩阵测试
- **覆盖率收集**：集成 pytest-cov，生成 XML 覆盖率报告
- **ruff 代码规范**：添加 `[tool.ruff]` 配置，启用 E/W/F/I/UP/B 规则集
- **覆盖率配置**：`.coveragerc` 排除测试/数据/脚本目录
- **Coordinator 测试套件**：26 个测试覆盖 `_tool_loop`/`_execute_tool_calls`/`_reflect_phase`/`_handle_slash_command`/`_dispatch_smart`
- **执行器/事件测试套件**：19 个测试覆盖 ExecutorResult 属性、BaseExecutor 继承、EventBus payload 兼容

### Changed
- **版本号统一**：pyproject.toml / one_agent.py / API health 端点 / skills fallback 全部统一为 `2.0.0`
- **默认配置清理**：`default_config.yaml` 移除测试值（`One-Agent-Test` → `One-Agent`，`WARNING` → `INFO`，`Asia/Shanghai` → `UTC`）
- **AlertManager 架构**：从独立类改为继承 `Plugin`，通过 PluginManager 统一管理生命周期
- **HTTP 错误响应统一**：所有异常处理器返回 `{"error": {"code": ..., "message": ..., "type": ...}}` 结构
- **EventBus payload 兼容**：`publish()` 支持扁平 dict 和嵌套 dict 两种发布方式
- **导入顺序规范**：`one_agent.py` 修复 logger 定义穿插在导入中间的问题
- **README API 端点**：从 13 个扩展到完整的 59 个，按类别分组

### Fixed
- **`sqlite3` 未导入**：`api/__init__.py` 健康检查使用 `sqlite3.Error` 但未导入
- **`os` 引用前赋值**：`api/__init__.py` 文档上传端点因局部 `import os.path` 导致 `os.unlink` 报 F823
- **`os` 未导入**：`executors/__init__.py` 进程组终止使用 `os.killpg` 但未导入 `os`
- **f-string 反斜杠**：`skills/__init__.py` 在 f-string 中使用 `\u4e00` 转义，Python 3.10 不兼容
- **`RISK_LABELS` 未定义导出**：`executors/system.py` 的 `__all__` 引用类属性而非模块级名称
- **`__del__` 资源清理**：AuditLog/OfflineQueue/SQLiteConnectionPool 添加显式 `close()` 方法，`__del__` 仅作兜底
- **冗余事件类型别名**：删除 `turn_complete`/`approval_requested`/`cron_triggered` 三个冗余别名
- **CI 测试覆盖不全**：之前仅运行 2/7 测试文件，现在运行全部
- **1302 个代码规范问题**：通过 ruff 自动修复 + 手动修复，降至 0

### Removed
- **冗余导入**：清理 12 个未使用的导入（`json`/`mimetypes`/`Any`/`Optional` 等）
- **死代码**：删除 `models/__init__.py` 中 3 处 `last_err` 赋值（写入但从未读取）
- **冗余局部导入**：`api/__init__.py` 中 3 处 `import os`/`import os.path` 局部导入（与顶层重复）

## [0.1.0] — 2026-06-10

### Added
- 初始发布
- 多网关支持（CLI/Web API/Telegram/企业微信/钉钉/飞书/Discord/Slack）
- 四层智能路由（trivial/simple/complex/expert）
- 三层记忆系统（短期/长期/知识图谱）
- 技能系统与插件市场
- 执行环境（Shell/Docker/Browser/Python）
- 定时任务调度
- 监控仪表盘
- 多模态功能（图像/语音）
- 数据加密（Fernet）
- 国际化（中/英）
