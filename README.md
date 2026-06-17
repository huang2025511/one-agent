# One-Agent v2

> Token-efficient self-evolving microkernel AI agent framework.
> 高效 Token 的自进化微内核 AI Agent 框架。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Status](https://img.shields.io/badge/status-release-brightgreen)]()
[![Providers](https://img.shields.io/badge/providers-94%2B-orange)]()

---

## Quick Start / 快速开始

**3 步上手：**

```bash
# 1. 安装
git clone <repo-url> && cd one-agent
pip install -r requirements.txt

# 2. 配置 API Key（以商汤 SenseNova 为例）
export SENSENOVA_API_KEY=sk-xxxxx
# 或编辑 config/default_config.yaml → llm.api_keys.sensenova

# 3. 运行
python one_agent.py
```

启动后进入交互 CLI 界面，同时自动启动三端口：
- **Web UI** → `http://127.0.0.1:18791`
- **REST API** → `http://127.0.0.1:18792/docs`
- **Monitor Dashboard** → `http://127.0.0.1:18793`

用自然语言操作即可：`搜索今天的天气`、`计算 1234 * 5678`、`帮我写一个 Python 冒泡排序`、`查看模型列表`、`智能分层`、`状态`、`指标`……

---

## Architecture / 架构

```
                           ┌────────────────────────────────┐
                           │          OneAgentApp           │
                           │       (one_agent.py)           │
                           └──────────────┬─────────────────┘
                                          │
                    ┌─────────────────────┼─────────────────────┐
                    │                     │                     │
              ┌─────▼─────┐       ┌──────▼──────┐       ┌──────▼──────┐
              │  CLI       │       │  Web UI     │       │  REST API   │
              │  gateways/ │       │  gateways/  │       │  api/       │
              └─────┬─────┘       └──────┬──────┘       └──────┬──────┘
                    │                     │                     │
                    └─────────┬───────────┴───────────┬─────────┘
                              │                       │
                    ┌─────────▼──────────┐   ┌────────▼─────────┐
                    │    EventBus        │   │  PluginManager    │
                    │  (async + DLQ)     │   │  (topo-sorted)    │
                    └─────────┬──────────┘   └────────┬─────────┘
                              │                       │
              ┌───────────────┼───────────────┬───────┴───────────┐
              │               │               │                    │
        ┌─────▼─────┐   ┌─────▼─────┐   ┌─────▼─────┐      ┌─────▼──────┐
        │  Router   │──▶│Coordinator│   │  Memory   │      │  Skills    │
        │ 4-tier    │   │ Think→Act │   │ 3-tier    │      │ Manager    │
        └─────┬─────┘   └─────┬─────┘   └───────────┘      └─────┬──────┘
              │               │                                   │
              │         ┌─────▼─────┐                             │
              │         │ LLMProvider│                            │
              │         │ 94+ providers                           │
              │         └─────┬─────┘                             │
              │               │                                   │
              └───────┬───────┘                                   │
                      │                                           │
            ┌─────────▼─────────┐              ┌──────────────────▼──┐
            │   Executors       │              │  Scheduler / Monitor │
            │ (shell/docker/    │              │  Alert / Multimodal  │
            │  browser)         │              └──────────────────────┘
            └───────────────────┘
```

**数据流**：用户消息 → EventBus → Router（复杂度分类 + 模型选择）→ Coordinator（Think → Act 循环）→ LLM（含工具调用）→ 回复

---

## Core Features / 核心功能

### 1. Auto-adaptation / 自动适配
提供 provider 名称 + API Key 即可自动发现 endpoint、模型列表与能力。已内置 **94+ 提供商注册表**（OpenAI、Anthropic、Google、DeepSeek、商汤、智谱、月之暗面/Kimi、豆包、百川、StepFun……），**77 个候选 URL 并行探测**，支持中文提供商别名（商汤→sensenova、文心一言→wenxin、通义千问→qwen 等）。

### 2. Plugin Architecture / 插件架构
所有子系统均通过 `PluginManager` 可插拔管理（skills、router、models、api、gateways、memory 等），支持自动发现、拓扑排序、依赖校验。

### 3. Event-driven / 事件驱动
基于 `EventBus` 的微内核设计，内置 **DLQ（死信队列）**、异步分发、优先级调度、背压控制、消息全生命周期追踪。

### 4. Think Phase / 思考阶段
ReAct 风格执行器，每个回合先让 LLM 输出思考过程（Think），再进入工具调用循环（Act）。思考内容同时提供给前端展示。

### 5. 3-level LLM Degradation / 三级降级
API 兼容性自动降级：`tools → no-tools → minimal-prompt`，当模型不支持工具调用或返回 400 时自动回退，保证成功率。

### 6. Smart Failure Tracking / 智能失败追踪
技能连续失败 **3 次** 后自动标记为不可用，提示模型停止调用该工具、改用自有知识完成回答。

### 7. Web Search / 网页搜索
内置多源搜索：**DuckDuckGo → Bing** 自动切换，纯 HTML 解析，**完全不需要 API Key**。

### 8. Model Capability Detection / 模型能力检测
从模型名称自动识别 14+ 类能力：vision、video、image_generation、audio_in、audio_out、code、reasoning、embeddings、tools、json_mode、streaming、long_context、multilingual、fine_tune。

### 9. 4-tier Model Classification / 四层模型分类
所有模型按复杂度自动分配到四层：`trivial` → `simple` → `complex` → `expert`，Router 按任务复杂度自动选择最经济模型。

### 10. Recommendation Engine / 推荐引擎
跨 **12 个类别** 的模型推荐：`best_paid`、`best_free`、`best_for_vision`、`best_for_code`、`best_for_reasoning`、`best_for_audio` 等。

### 11. Multi-port / 多端口服务
单进程同时运行三个端口：
| 端口 | 服务 | 用途 |
|------|------|------|
| 18792 | REST API | 外部集成、自动化 |
| 18791 | Web UI | 浏览器交互 |
| 18793 | Monitor | 实时监控面板 |

### 12. Long-term Memory / 长期记忆
基于 **SQLite FTS5** 全文搜索的跨会话记忆，支持**权重衰减**、分页查询、相关性阈值，WAL 模式保证并发读写。

### 13. Config Backup / 配置备份
版本化配置备份，API/自然语言一键创建/恢复/删除，原子写入保证安全。

### 14. i18n / 国际化
中英文双语支持，**自动检测用户语言**并切换界面/系统提示词，语言偏好持久化到配置文件。

### 15. Security / 安全
- Shell 执行器带 **正则白名单**，危险命令（`rm -rf`、`sudo`、`git push --force` 等）被自动拒绝
- REST API 带 **速率限制** + **请求体大小上限** + API Key 认证（恒时比较防时序攻击）
- 加密存储支持（Fernet）+ 敏感配置写入保护

---

## Configuration Reference / 配置参考

主配置文件：`config/default_config.yaml`（YAML 格式，含完整注释）

```yaml
agent:
  name: One-Agent
  version: 2.0.0
  data_dir: ./data
  log_level: INFO          # DEBUG | INFO | WARNING | ERROR
  timezone: UTC
  language: en             # en | zh

llm:
  primary_provider: sensenova
  primary_model: sensenova/deepseek-v4-flash
  lightweight_model: sensenova/sensenova-6.7-flash-lite
  api_keys:
    openrouter: null
    openai: null
    anthropic: null
    deepseek: null
    dashscope: null
    sensenova: ${SENSENOVA_API_KEY}    # 支持 ${ENV_VAR} 环境变量引用
  default_temperature: 0.3
  default_max_tokens: 2048
  timeout: 30
  retries: 1
  cost_tracking:
    daily_budget: 1.0
    monthly_budget: 20.0

router:
  enabled: true
  task_complexity_thresholds:
    trivial: 0.2  simple: 0.5  complex: 0.8  expert: 1.0
  context_compression:
    enabled: false
  self_evolution:
    enabled: false

memory:
  short_term: {max_turns: 20, max_tokens: 8000}
  long_term:
    enabled: true
    storage: sqlite-fts5
    decay_enabled: true

rest:
  enabled: true
  host: 127.0.0.1
  port: 18792
  rate_limit_per_minute: 60
  max_chat_bytes: 65536
  cors_origins: ['http://localhost', 'http://127.0.0.1']
```

更多配置项见 `config/default_config.yaml` 文件内注释。

---

## API Reference / API 参考

REST API 运行在 `http://127.0.0.1:18792`，Swagger 文档在 `/docs`。共 **59 个端点**：

### 对话与流式
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | 发送消息 → 获取回复（含思考过程） |
| POST | `/api/chat/stream` | 流式聊天（SSE） |

### 健康检查与监控
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 基础存活检查 |
| GET | `/ready` | 就绪检查（含数据库连通性） |
| GET | `/api/health` | 增强健康检查（含子系统状态） |
| GET | `/api/health/ready` | K8s readiness 探针 |
| GET | `/api/health/live` | K8s liveness 探针 |
| GET | `/api/stats` | 系统统计 |
| GET | `/api/metrics` | 指标聚合（Bus + LLM + Memory） |
| GET | `/metrics` | Prometheus 格式指标 |
| GET | `/dashboard` | 监控面板 HTML |

### 记忆与会话
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/memory/search?q=…` | 搜索长期记忆 |
| POST | `/api/memory/add` | 添加记忆 |
| GET | `/api/memory/page` | 分页记忆列表 |
| GET | `/api/sessions` | 会话列表 |
| GET | `/api/sessions/list` | 会话列表（别名） |
| GET | `/api/sessions/{id}` | 获取会话详情 |
| DELETE | `/api/sessions/{id}` | 删除会话 |
| POST | `/api/sessions/{id}/fork` | 从指定位置分叉会话 |
| GET | `/api/sessions/{id}/tree` | 获取会话分支树 |

### 技能与市场
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/skills` | 列出所有技能 |
| GET | `/api/marketplace` | 浏览技能市场 |
| POST | `/api/marketplace/publish` | 发布技能到市场 |
| POST | `/api/marketplace/install` | 从市场安装技能 |
| DELETE | `/api/marketplace/{name}` | 卸载市场技能 |

### 配置管理
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/config` | 获取当前配置 |
| POST | `/api/config/reload` | 热重载配置 |
| GET | `/api/settings?key=…` | 读取配置项 |
| POST | `/api/settings` | 修改配置项 |
| GET | `/api/config/backups` | 列出配置备份 |
| POST | `/api/config/backup` | 创建配置备份 |
| POST | `/api/config/restore` | 恢复配置备份 |
| GET | `/api/config/backups/{filename}` | 下载配置备份 |
| DELETE | `/api/config/backups/{filename}` | 删除配置备份 |

### 文档 RAG
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/documents/ingest` | 导入文档（支持 txt/md/pdf） |
| GET | `/api/documents` | 列出已导入文档 |
| GET | `/api/documents/search?q=…` | 搜索文档内容 |
| DELETE | `/api/documents/{name}` | 删除已导入文档 |

### 成本追踪
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/costs/daily` | 每日成本 |
| GET | `/api/costs/monthly` | 每月成本 |
| GET | `/api/costs/budget` | 预算使用情况 |
| GET | `/api/costs/by-provider` | 按 provider 分组成本 |
| GET | `/api/costs/recent` | 最近成本记录 |

### 告警与审批
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/alerts/rules` | 告警规则列表 |
| POST | `/api/alerts/rules` | 创建告警规则 |
| DELETE | `/api/alerts/rules/{name}` | 删除告警规则 |
| GET | `/api/alerts/history` | 告警历史 |
| GET | `/api/approvals/pending` | 待审批请求 |
| POST | `/api/approvals/{id}/approve` | 批准请求 |
| POST | `/api/approvals/{id}/deny` | 拒绝请求 |

### MCP 客户端
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/mcp/tools` | 列出 MCP 工具 |
| POST | `/api/mcp/call` | 调用 MCP 工具 |
| POST | `/api/mcp/add-server` | 添加 MCP 服务器 |
| DELETE | `/api/mcp/servers/{name}` | 移除 MCP 服务器 |

### 审计与自改进
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/audit` | 审计日志 |
| GET | `/api/audit/stats` | 审计统计 |
| GET | `/api/improvements` | 自改进建议 |
| GET | `/api/improvements/failures` | 失败模式分析 |

### 缓存
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/cache/clear` | 清除 LLM 缓存 |

**示例请求：**

```bash
curl -X POST http://127.0.0.1:18792/api/chat \
  -H "Content-Type: application/json" \
  -d '{"text": "用 Python 写一个快速排序", "session_id": "demo"}'
```

返回：
```json
{
  "reply": "以下是 Python 快速排序的实现…",
  "session_id": "demo",
  "thinking": "用户要求用 Python 写快速排序，这是一个中等复杂度的编程任务…"
}
```

---

## Testing / 测试

```bash
# 冒烟测试（零网络依赖，验证全链路）
python tests/smoke.py

# 单元测试
python -m pytest tests/ -v

# Benchmark 测试
python tests/benchmark_test.py
```

冒烟测试涵盖：模块导入 → LLM 缓存 LRU+TTL → EventBus DLQ → Shell 安全白名单 → Memory 分页 → 插件自动发现 → Coordinator 全链路（含 Stub LLM）。

---

## Project Structure / 项目结构

```
one_agent/
├── one_agent.py              入口 & OneAgentApp 装配
├── pyproject.toml            项目元信息
├── requirements.txt          依赖清单
├── install                    安装脚本
├── config/
│   ├── default_config.yaml   默认配置
│   └── test_config.yaml      测试配置
├── core/                     微内核
│   ├── context.py            AgentContext / TurnContext
│   ├── events.py             EventBus（DLQ + 追踪 + 指标）
│   ├── plugin.py             Plugin / PluginManager（自动发现）
│   └── coordinator.py        Coordinator（Think→Act 循环）
├── models/                   LLM 层
│   ├── __init__.py           LLMProvider + LLMCache（LRU+TTL）
│   ├── resolver.py           94+ 提供商注册表 + 自动探测
│   ├── catalog.py            ModelCatalog（模型列表 + 过滤）
│   └── capabilities.py       14 类能力检测
├── router/
│   └── __init__.py           4 层智能路由 + 自进化
├── memory/
│   └── __init__.py           3 层记忆（short / long FTS5 / procedural）
├── skills/
│   └── __init__.py           SkillManager + 内置技能（calc/echo/web_search/settings…）
├── executors/
│   └── __init__.py           Shell / Docker / Browser 执行器
├── api/
│   └── __init__.py           FastAPI REST 网关（20+ 端点）
├── gateways/
│   ├── __init__.py           CLI / Web UI / Telegram / WeCom / DingTalk / Feishu / Discord / Slack
│   └── index.html            Web UI 单页应用
├── scheduler/
│   └── __init__.py           APScheduler 定时任务
├── monitor/
│   └── __init__.py           实时监控面板
├── multimodal/
│   └── __init__.py           多模态支持
├── marketplace/
│   └── __init__.py           技能市场框架
├── alerting/
│   └── __init__.py           告警管理（规则 + 历史）
├── i18n/
│   └── __init__.py           国际化（中/英，自动检测）
├── config_backup/
│   └── __init__.py           配置备份/恢复
├── tests/                    测试套件
│   ├── smoke.py              冒烟测试
│   ├── unit_tests.py         单元测试
│   ├── benchmark_test.py     Benchmark
│   └── e2e_test.py           端到端测试
└── data/                     运行时数据（logs / memory / marketplace）
```

## Requirements / 依赖

- **Python**: 3.10+
- **Runtime**: httpx, aiohttp, pydantic (≥2.5), pyyaml, fastapi, uvicorn, APScheduler, cryptography
- **Test**: pytest-asyncio

## Design Principles / 设计原则

1. **零魔法** — 每个模块精简（300 行左右），通过事件总线通信，不引入 LangChain 等黑盒框架。
2. **成本优先** — 按任务难度选模型，按需加载技能，不做全量上下文注入。
3. **可插拔** — 任一子系统（LLM、记忆、调度、网关）可单独替换。
4. **本地第一** — 所有状态（对话历史、记忆索引、生成的技能）默认写入 `data/`，用户可审计、可移植。

## Documentation / 文档

- [CHANGELOG.md](CHANGELOG.md) — 版本更新记录
- [CONTRIBUTING.md](CONTRIBUTING.md) — 贡献指南与开发规范
- [TUTORIAL.md](TUTORIAL.md) — 安装与使用教程
- [CODE_AUDIT_REPORT.md](CODE_AUDIT_REPORT.md) — 代码审计报告

## License

MIT