# One-Agent — `one-agent`

> 融合 **OpenClaw** + **Hermes Agent** + **OpenSquilla** 三者设计优势的开源 AI Agent。
> Token-efficient, 微内核事件驱动, 三层记忆, MetaSkill 生态。

## 架构总览

```
┌─────────────── 微内核 EventBus (core/events.py, plugin.py) ───────────────┐
│                                                                            │
│   gateways  ─► user_message ─► router ─► coordinator ─►  LLM (20+ 后端)    │
│   (CLI/Telegram/WebUI)           │                    │                    │
│                                   ▼                    ▼                    │
│                               memory 3-tier       skills + executors        │
│                             (short/long/proc)   (MetaSkill / MCP / shell/docker) │
│                                                                            │
└────────────────  scheduler (APScheduler, 主动式 cron) ─────────────────────┘
```

## 特性清单

| 维度 | 实现 | 借鉴来源 |
|---|---|---|
| 微内核事件总线 | `core/events.py` + `core/plugin.py` | OpenSquilla |
| 4 层 Token 智能调度 | `router/` (复杂度分类 / 上下文压缩 / 技能懒加载 / 自进化) | OpenSquilla |
| 3 层记忆 | `memory/` (short / FTS5 long-term / procedural auto-skill) | Hermes Agent |
| MetaSkill + MCP | `skills/` (Markdown + YAML + 动态 MCP server) | OpenClaw + OpenSquilla |
| 多平台接入 | `gateways/` (CLI / Telegram / Web UI / Discord & Slack hooks) | OpenClaw + Hermes |
| 多执行环境 | `executors/` (shell / Docker / 浏览器 headless) | OpenClaw |
| 主动式 cron | `scheduler/` (APScheduler) | OpenClaw |
| LLM 提供商抽象 | `models/` (OpenRouter / OpenAI / Anthropic / DeepSeek / Ollama) | Hermes |
| 安全沙箱 | 可配置 allow-list + Docker 隔离 | OpenClaw |

## 快速开始

```bash
# 1. 安装依赖（最小版）
pip install pyyaml httpx

# 完整依赖（含 web UI + scheduler）
pip install -r requirements.txt

# 2. 配置 API Key
export OPENROUTER_API_KEY=sk-...
# 或在 config/default_config.yaml 中直接指定

# 3. 启动
python one_agent.py
#  → 交互 CLI
#  → 同时启动 Web UI:  http://127.0.0.1:18791/
```

## 冒烟测试

```bash
python tests/smoke.py
```

这会跑一个零网络依赖的端到端测试，验证：难度分类 → 模型路由 → 记忆注入 → 技能懒加载 → 回答生成全链路工作。

## 配置参考

主配置文件：[`config/default_config.yaml`](config/default_config.yaml)，可调：

- 启用/禁用某个 LLM 后端
- 路由阈值（trivial/simple/complex/expert）
- 记忆系统的各层参数
- 是否允许 shell 执行、Docker 沙箱
- Telegram Bot / Discord / Slack token
- 定时任务（cron）

## 目录结构

```
one_agent/
├── one_agent.py                入口 & 插件装配
├── config/default_config.yaml  主配置
├── core/                    微内核 (events / plugin / context / coordinator / agent)
├── models/                  LLM 提供商抽象
├── router/                  4 层智能调度
├── memory/                  3 层记忆系统
├── skills/                  MetaSkill + MCP
├── executors/               shell/Docker/浏览器执行器
├── gateways/                CLI / Telegram / Web UI
├── scheduler/               主动式 cron 调度
└── tests/smoke.py           端到端冒烟测试
```

## 设计原则

1. **零魔法** — 每个模块在 300 行以内，通过事件总线通信；不引入 LangChain / AutoGen 之类的黑盒框架。
2. **成本优先** — 不把全量上下文塞进每一次 LLM 调用。路由层按难度切模型，按任务按需加载技能。
3. **可插拔** — 任何一个子系统（LLM、记忆、调度、网关）都可以单独替换而不影响其他模块。
4. **本地第一** — 所有状态（对话历史、记忆索引、生成的技能）默认写在本地 `data/`，用户可审计、可移植。

## 安全

- 本地 shell 执行默认 **禁用**；启用时默认带 command allow-list。
- Docker 执行器在只读容器内运行，无网络、无卷挂载。
- 所有本地状态写入 `data/`，在 `.gitignore` 内被排除。
- 请不要把 `.env` / token 提交到 git。

## License

MIT — 与上游项目保持一致。
