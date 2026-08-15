# Changelog

All notable changes to One-Agent are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.2.0] — 2026-08-15

### Added
- **数据库维护套件（`core/db_maintenance.py`）**
  - `db_stats`：库大小 / 表行数 / schema 版本 / 待迁移遗留库 / 备份概要一站式统计
  - `integrity_check`：`PRAGMA quick_check` 快速完整性检查 + WAL 残留体积
  - `vacuum`：空间回收，VACUUM 后强制 WAL checkpoint 截断主文件（修复 WAL 模式下文件不缩小的陷阱）
  - `run_auto_backup`：checkpoint → zip（内含 `one_agent.db`）→ 可选加密 → 轮换保留 N 份；同秒重复触发自动加序号防覆盖
  - `DBMaintenancePlugin`：接入调度器，每日 04:30 自动备份（`scheduler.db_maintenance` 配置节，默认保留 7 份）
- **备份加密**：设置 `ONE_AGENT_DB_KEY` 后，备份 zip 内的库文件自动以 Fernet 加密为 `one_agent.db.enc`；
  `import --restore-db` 用同一口令自动解密整库还原。`cryptography` 缺失时降级明文备份并告警（显式要求加密则报错）
- **SQLCipher 可选运行库加密**：`ONE_AGENT_DB_CIPHER=1` 且安装 pysqlcipher3 时，统一库启用 SQLCipher 透明加密（默认关闭）
- **CLI 数据管理子命令**
  - `one-agent export`：导出全部数据为 zip（含统一库，可异地部署）
  - `one-agent import`：从备份导入；`--restore-db` 整库还原（现库自动另存 `one_agent.db.pre_import` 兜底）
  - `one-agent migrate`：旧版分散库迁入统一库；`--dry-run` 只读预览（表数 / 行数）
  - `one-agent db stats|check|vacuum|backup`：统一库日常维护
  - `doctor` 集成 integrity_check + 备份概要
- **数据库维护 REST API**：`GET /api/db/stats`（统计）、`GET /api/db/check`（完整性）、`GET /api/db/backups`（备份列表）、`POST /api/db/backup`（立即备份）

### Changed
- **Dockerfile**：声明 `VOLUME /app/data`，容器化部署不再丢数据，与"复制一个数据库即可迁移"的架构目标对齐
- **迁移幂等性增强（`core/hub.py`）**：`migrate_legacy` 支持 `dry_run` 预览；目标表已有数据时跳过复制，杜绝重复导入覆盖新数据

### Fixed
- **`core/sub_agent.py`**：critic 审查调用的 tokens 从未计入 `total_tokens`（成本统计遗漏）
- **`core/coordinator_tasks.py`**：`List` 未导入（F821，触发即 NameError）
- **`core/reasoning.py`**：`re` 未导入（F821）
- **`core/task_scheduler.py` / `monitor/tracing.py`**：`sqlite3` / `Callable` 未导入（F821 类型注解）
- **`tests/unit_tests.py`**：`main()` 调用两个早已删除的 sub-agent 测试函数（运行即 NameError）
- **`skills/__init__.py`**：`_MAGIC` 魔数表 `%PDF` 十六进制写法重复键（死键）
- **`monitor/prometheus.py` 等 10 处**：`zip()` 显式 `strict=False`，行为不变、意图明确
- **全库 ruff 告警 463 → 0**：未使用导入 / 死变量 / 循环变量绑定（B023 闭包默认参数显式绑定）/ 空白行规范化；6 处依赖探测导入加 noqa 说明

## [2.1.0] — 2026-08-15

### Added
- **统一数据库中枢（`core/hub.py`）**：所有系统状态收拢到 `{data_dir}/one_agent.db` 单一文件
  - 收拢内容：配置（settings）/ 会话 / 长期记忆 / 知识图谱 / Embeddings / 角色 / 用户画像 / 审批 / 审计 / 任务 / 改进 / 成本 / 文档索引 / KV / 文件包 / 配置备份
  - 旧版分散库（`config.db`、`memory/*.db`）首次启动自动 ATTACH 迁移到统一库，原文件改名 `*.migrated` 兜底；迁移过程幂等，可重复执行
  - `schema_versions(store, version)` 表替代单值 `user_version`，支持多 store 共库
  - `kv_get/kv_set/files_put/files_get/materialize/capture_dir/backup_*/checkpoint` 统一接口
  - 文件库权限 0600，凭据与配置同等敏感
- **配置库（`core/config_store.py`）**：YAML 仅作首启种子，运行期配置全部走 SQLite
  - 三级解析：代码默认值 < YAML 种子 < 数据库覆盖 < 环境变量展开
  - 线程安全（RLock + 进程内单例）；`overlay_enabled()` 按加载源判断是否叠加
- **数据目录解析统一（`core/hub.resolve_data_dir`）**：`ONE_AGENT_DATA_DIR` > `agent.data_dir` > `./data`；所有存储点共用同一目录，不再分裂
- **审批持久化（`core/approval.py`）**：审批请求 + 历史记录落 SQLite，重启不丢待办；TTL 过期自动 deny
- **API 层数据目录修复**：所有路由统一使用 `resolve_data_dir` 解析数据目录，消除"读 dev / 写 default"路径错位
- **统一数据库测试（`tests/test_unified_db.py`）**：并发访问 / 备份导出导入 / 自定义 provider 迁移 / 跨环境 round-trip
- **备份导出导入（`core/backup_export.py`）**：导出 ZIP 直接附 `one_agent.db`，新环境导入即部署

### Changed
- **版本号统一**：`pyproject.toml` / `one_agent.py` / `api/__init__.py`（健康检查 + FastAPI app）/ `monitor/__init__.py` / `skills/__init__.py`（version 命令 fallback）/ `config/default_config.yaml` / `config/test_config.yaml` / `core/setup_wizard.py` 全部对齐为 `2.1.0`，避免硬编码漂移
- **配置库文件权限 0600**：防止多用户系统泄露 API key / 聊天记录
- **`.gitignore` 完善**：补全 `data/` 整目录规则，删除重复 `test-report.log` 条目；防止 `one_agent.db` 等敏感数据被误提交
- **同步脚本 remote 名称修复**：`sync_push.sh` / `sync_pull.sh` 的 `origin` = Gitee / `github` = GitHub，修复旧脚本假设的 `gitee` / `orient` 错误名称
- **Web UI 单页 `gateways/index.html` 删除**：REST API 才是网关标准入口；如需自定义页面扩展 FastAPI 即可
- **数据库路径**：`memory/knowledge_graph.py` / `memory/embeddings.py` / `core/self_improve.py` 默认数据库路径统一指向 `one_agent.db`
- **微信凭据目录**：`gateways/wechat_personal.py` 凭据统一到 `{data_dir}/wechat_*.pkl`，支持旧路径自动迁移

### Fixed
- **数据目录解析分裂**：`OneAgentApp.__init__` 一次性解析 `self.data_dir`，所有存储点统一使用
- **测试数据目录污染**：`conftest.py` 每个测试用例独立临时数据目录，结束关闭连接
- **配置覆盖规则错误**：`overlay_enabled` 改为按加载的 yaml 是否在项目 `config/` 目录判断，确保单测自建 yaml 完全接管配置
- **微信凭据目录迁移测试污染**：通过函数参数注入 legacy 路径，避免模块级变量修改
- **审批 TTL 持久化**：重启后扫描过期请求，标记为 deny，避免幽灵待办
- **`api/__init__.py` 重复 `import os`**：删除函数内重复导入（已在顶层导入）
- **`gateways/__init__.py` 死代码**：删除 `index.html` 探测逻辑 + 未使用的 `from pathlib import Path` + `HTMLResponse` 导入

### Removed
- **`gateways/index.html`**：100+ 行静态 HTML UI，REST API 已是网关标准入口
- **重复 `import os`**：`api/__init__.py._resolve_config_path` 局部导入（与顶层重复）
- **`__pycache__` 与 `test-results.xml`**：本地开发残留物（已在 `.gitignore`）

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
