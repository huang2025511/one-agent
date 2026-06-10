# Athena Agent 安装与使用教程

> 本教程将带你从零开始安装、配置并运行 Athena Agent，覆盖所有主要功能的使用方法。

---

## 目录

1. [环境要求](#1-环境要求)
2. [安装步骤](#2-安装步骤)
3. [配置 API Key](#3-配置-api-key)
4. [启动 Agent](#4-启动-agent)
5. [CLI 交互模式](#5-cli-交互模式)
6. [Web UI 聊天界面](#6-web-ui-聊天界面)
7. [REST API 接口](#7-rest-api-接口)
8. [Telegram Bot 接入](#8-telegram-bot-接入)
9. [企业微信接入](#9-企业微信接入)
10. [四层智能路由详解](#10-四层智能路由详解)
11. [三层记忆系统](#11-三层记忆系统)
12. [技能系统](#12-技能系统)
13. [执行环境（Shell / Docker / 浏览器）](#13-执行环境shell--docker--浏览器)
14. [定时任务调度](#14-定时任务调度)
15. [监控仪表盘](#15-监控仪表盘)
16. [多模态功能（图像/语音）](#16-多模态功能图像语音)
17. [插件市场](#17-插件市场)
18. [数据加密](#18-数据加密)
19. [冒烟测试](#19-冒烟测试)
20. [常见问题](#20-常见问题)

---

## 1. 环境要求

| 项目 | 最低要求 | 推荐 |
|------|---------|------|
| Python | 3.10+ | 3.12 |
| 操作系统 | Linux / macOS / Windows | Linux |
| 内存 | 512 MB | 2 GB+ |
| 磁盘 | 100 MB | 1 GB+（含数据） |
| Docker | 可选 | 24+（如需沙箱执行） |

---

## 2. 安装步骤

### 2.1 克隆项目

```bash
git clone https://github.com/huang2025511/agnet.git
cd agnet
```

### 2.2 创建虚拟环境（推荐）

```bash
python -m venv venv

# Linux / macOS
source venv/bin/activate

# Windows
venv\Scripts\activate
```

### 2.3 安装依赖

**最小安装**（仅 CLI + LLM 调用）：

```bash
pip install pyyaml httpx
```

**完整安装**（含 Web UI、调度器、缓存等全部功能）：

```bash
pip install -r requirements.txt
```

完整依赖列表：

| 包 | 用途 |
|----|------|
| `httpx` | HTTP 客户端（LLM API 调用） |
| `pydantic` | 配置验证 |
| `pyyaml` | YAML 配置解析 |
| `fastapi` + `uvicorn` | Web UI / REST API / 监控仪表盘 |
| `APScheduler` | 定时任务调度 |
| `cryptography` | API Key Fernet 加密 |
| `jinja2` | 模板渲染 |
| `rich` | 终端美化输出 |

> 如果某个可选依赖安装失败（如 `APScheduler`），对应功能会被自动禁用，不影响其他功能使用。

---

## 3. 配置 API Key

Athena 支持 6 个 LLM 提供商，至少配置一个即可使用。

### 3.1 通过环境变量（推荐，最安全）

```bash
# 选择一个或多个配置
export OPENROUTER_API_KEY="sk-or-v1-..."      # OpenRouter（推荐，一个 Key 访问所有模型）
export OPENAI_API_KEY="sk-..."                 # OpenAI 官方
export ANTHROPIC_API_KEY="sk-ant-..."          # Anthropic Claude
export DEEPSEEK_API_KEY="sk-..."               # DeepSeek
export DASHSCOPE_API_KEY="sk-..."              # 阿里云百炼
# Ollama 无需 Key，本地运行
```

也可以写入 `.env` 文件（已被 `.gitignore` 忽略）：

```bash
cat > .env << 'EOF'
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxx
EOF
```

### 3.2 通过配置文件

编辑 `config/default_config.yaml`：

```yaml
llm:
  api_keys:
    openrouter: "sk-or-v1-你的Key"
    anthropic: "sk-ant-你的Key"
```

> 配置文件中的 `${ENV_VAR}` 语法会自动从环境变量读取，两种方式可以混用。

### 3.3 选择默认模型

```yaml
llm:
  primary_provider: "openrouter"          # 主要提供商
  primary_model: "anthropic/claude-3.5-sonnet"  # 默认模型
```

支持的模型格式为 `提供商/模型名`，例如：
- `anthropic/claude-3.5-sonnet-20241022`
- `openai/gpt-4o`
- `deepseek/deepseek-chat`
- `qwen/qwen-2.5-7b-instruct`

---

## 4. 启动 Agent

```bash
# 使用默认配置
python athena.py

# 指定配置文件
ATHENA_CONFIG=./my_config.yaml python athena.py
```

启动后会看到：

```
╔══════════════════════════════════════════╗
║  Athena v2 — enter a message, or 'exit'  ║
╚══════════════════════════════════════════╝
athena>
```

同时自动启动的服务：

| 服务 | 地址 | 说明 |
|------|------|------|
| Web UI | http://127.0.0.1:18791 | 聊天界面 |
| REST API | http://0.0.0.0:18792 | 外部集成接口 |
| 监控仪表盘 | http://127.0.0.1:18793 | 系统状态面板 |

---

## 5. CLI 交互模式

CLI 是最直接的使用方式：

```
athena> 你好，请介绍一下自己
athena> 帮我写一个 Python 快速排序
athena> exit
```

### 内置命令

| 命令 | 功能 |
|------|------|
| `exit` / `quit` / `q` | 退出程序 |
| `help` / `?` | 显示帮助 |
| `skills` | 列出已加载技能 |
| `status` | 显示内存/LLM/总线状态 |
| `stats` | 显示详细统计 |
| `metrics` | 显示总线 + LLM + 记忆指标 |
| `dlq` | 查看死信队列 |

### 优雅退出

按 `Ctrl+C` 或输入 `exit`，Agent 会依次停止所有插件、关闭数据库连接、清理资源。

---

## 6. Web UI 聊天界面

启动后浏览器访问 **http://127.0.0.1:18791/**

功能：
- 实时对话，自动维持会话上下文
- 深色主题，移动端适配
- 无需额外安装，开箱即用

修改端口/地址：

```yaml
gateways:
  web:
    enabled: true
    host: "0.0.0.0"    # 允许外部访问
    port: 8080          # 自定义端口
```

---

## 7. REST API 接口

REST API 适合外部程序集成，默认运行在 `http://0.0.0.0:18792`。

### 7.1 设置 API Key 认证

```bash
# 方式一：环境变量
export ATHENA_API_KEY="my-secret-key"

# 方式二：配置文件
```

```yaml
gateways:
  rest:
    api_key: "my-secret-key"
```

### 7.2 接口列表

**聊天**

```bash
curl -X POST http://localhost:18792/api/chat \
  -H "Content-Type: application/json" \
  -H "X-API-Key: my-secret-key" \
  -d '{"text": "你好", "session_id": "my-session"}'
```

响应：

```json
{"reply": "你好！有什么可以帮你的？", "session_id": "my-session"}
```

**搜索记忆**

```bash
curl "http://localhost:18792/api/memory/search?q=Python&limit=5" \
  -H "X-API-Key: my-secret-key"
```

**添加记忆**

```bash
curl -X POST http://localhost:18792/api/memory/add \
  -H "Content-Type: application/json" \
  -H "X-API-Key: my-secret-key" \
  -d '{"text": "用户偏好深色主题", "tags": "preference"}'
```

**分页浏览记忆**

```bash
curl "http://localhost:18792/api/memory/page?page=1&page_size=20" \
  -H "X-API-Key: my-secret-key"
```

**列出技能**

```bash
curl http://localhost:18792/api/skills -H "X-API-Key: my-secret-key"
```

**系统状态**

```bash
curl http://localhost:18792/api/stats -H "X-API-Key: my-secret-key"
```

**健康检查**（无需认证）

```bash
curl http://localhost:18792/api/health
# {"status": "ok", "uptime": 3600}
```

**清除 LLM 缓存**

```bash
curl -X POST http://localhost:18792/api/cache/clear \
  -H "X-API-Key: my-secret-key"
```

---

## 8. Telegram Bot 接入

### 8.1 创建 Bot

1. 在 Telegram 中搜索 `@BotFather`
2. 发送 `/newbot`，按提示创建
3. 获得 Bot Token（格式：`123456789:ABCdefGHI...`）

### 8.2 配置

```yaml
gateways:
  telegram:
    enabled: true
    bot_token: "123456789:ABCdefGHI..."
    allowed_users: ["12345678"]   # Telegram 用户 ID，留空允许所有人
```

或通过环境变量：

```bash
export TELEGRAM_BOT_TOKEN="123456789:ABCdefGHI..."
```

### 8.3 获取你的 Telegram 用户 ID

向 `@userinfobot` 发送任意消息即可获取。

### 8.4 使用

启动 Agent 后，Bot 会自动开始长轮询。在 Telegram 中向你的 Bot 发消息即可对话。

---

## 9. 企业微信接入

Athena 支持通过**企业微信（WeCom）**连接微信生态，提供两种模式。

### 9.1 Webhook 模式（群机器人，最简单）

在群聊中添加机器人，获取 Webhook Key 后即可向群聊推送消息。

**配置：**

```yaml
gateways:
  wecom:
    enabled: true
    mode: "webhook"
    webhook_key: "你的Webhook Key"
```

或环境变量：

```bash
export WECOM_WEBHOOK_KEY="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

**获取 Webhook Key：**
1. 打开企业微信群聊 → 右上角 `...` → 群机器人 → 添加机器人
2. 复制 Webhook 地址中的 `key=` 后面的值

**使用：**

```python
# 发送文本消息
result = await app.wecom.send_webhook("任务完成！")

# @指定人
result = await app.wecom.send_webhook("请查看", mentioned_list=["zhangsan"])

# @所有人
result = await app.wecom.send_webhook("紧急通知！", mentioned_list=["all"])

# 发送 Markdown 消息
result = await app.wecom.send_markdown(
    "## 日报\n> 完成项: 5\n> 待处理: 2"
)
```

### 9.2 App 模式（自建应用，可收发消息）

通过企业微信自建应用，实现**接收用户消息并自动回复**。

**前置条件：**
- 企业微信管理员权限
- 服务器有公网 IP（用于接收回调）

**配置步骤：**

1. 登录 [企业微信管理后台](https://work.weixin.qq.com/wework_admin/frame) → 应用管理 → 自建应用
2. 创建应用，获取 `AgentId` 和 `Secret`
3. 在"接收消息"栏设置回调 URL：`http://你的服务器:18794/wecom/callback`
4. 设置 Token 和 EncodingAESKey

**配置文件：**

```yaml
gateways:
  wecom:
    enabled: true
    mode: "app"
    corp_id: "ww1234567890"          # 企业 ID（我的企业 → 企业信息）
    agent_id: "1000002"               # 应用 AgentId
    secret: "你的应用Secret"           # 应用 Secret
    callback_token: "你的Token"        # 回调 Token
    encoding_aes_key: "你的AESKey"     # 回调 EncodingAESKey
    callback_host: "0.0.0.0"
    callback_port: 18794
```

或环境变量：

```bash
export WECOM_CORP_ID="ww1234567890"
export WECOM_AGENT_ID="1000002"
export WECOM_SECRET="你的Secret"
export WECOM_CALLBACK_TOKEN="你的Token"
export WECOM_ENCODING_AES_KEY="你的AESKey"
```

配置完成后，在企业微信中向应用发消息，Athena 会自动回复。

---

## 10. 四层智能路由详解

Athena 的路由系统会自动根据问题难度选择最合适的模型，节省 Token 开销。

### 10.1 四个难度等级

| 等级 | 复杂度 | 典型问题 | 模型示例 |
|------|--------|---------|---------|
| **trivial** | < 0.2 | "你好"、"几点了？" | Llama-3-8B, DeepSeek-Chat |
| **simple** | 0.2 - 0.5 | "Python 列表去重" | GPT-4o-mini, Claude Haiku |
| **complex** | 0.5 - 0.8 | "写一个 REST API" | Claude 3.5 Sonnet, GPT-4o |
| **expert** | > 0.8 | "优化分布式死锁" | Claude 4.5 Sonnet, o3 |

### 10.2 自定义阈值

```yaml
router:
  task_complexity_thresholds:
    trivial: 0.2
    simple: 0.5
    complex: 0.8
    expert: 1.0
```

### 10.3 上下文压缩

长对话自动压缩历史，减少 Token 消耗：

```yaml
router:
  context_compression:
    enabled: true
    min_tokens_before_compress: 2000
    compression_ratio: 0.4
```

### 10.4 自进化

路由器会根据历史对话的成功/失败率自动调整阈值：

```yaml
router:
  self_evolution:
    enabled: true
    min_samples_before_adjust: 50
```

---

## 11. 三层记忆系统

### 11.1 第一层：短期记忆

当前会话的最近对话，自动管理，无需配置。

```yaml
memory:
  short_term:
    max_turns: 20      # 保留最近 20 轮
    max_tokens: 8000   # 最大 Token 数
```

### 11.2 第二层：长期记忆

基于 SQLite FTS5 全文检索，跨会话持久化。

```yaml
memory:
  long_term:
    enabled: true
    storage: "sqlite-fts5"
    max_results: 5            # 每次检索最多返回 5 条
    relevance_threshold: 0.6  # 相关性阈值
    decay_enabled: true       # 旧记忆权重衰减
```

通过 API 手动添加/检索记忆：

```bash
# 添加
curl -X POST http://localhost:18792/api/memory/add \
  -H "Content-Type: application/json" \
  -d '{"text": "项目使用 FastAPI 框架", "tags": "tech_stack"}'

# 搜索
curl "http://localhost:18792/api/memory/search?q=框架"
```

### 11.3 第三层：程序记忆

Agent 会自动从重复出现的操作模式中生成可复用的技能（SKILL.md）：

```yaml
memory:
  procedural:
    enabled: true
    auto_create_skills: true       # 自动生成技能
    min_usage_before_skill: 3      # 至少出现 3 次才生成
    skill_storage: "markdown"
```

---

## 12. 技能系统

### 12.1 内置技能

Athena 自带 4 个内置技能：

| 技能 ID | 功能 | 示例 |
|---------|------|------|
| `echo` | 回显输入（调试用） | "echo hello" |
| `now` | 返回当前时间 | "现在几点" |
| `calc` | 安全数学计算 | "计算 2^10 + 3*5" |
| `save_note` | 保存笔记到文件 | "记录：项目截止日期是6月底" |

### 12.2 自定义 Markdown 技能

在 `data/skills/user/` 目录下创建 `.md` 文件：

```markdown
---
id: translate
title: 翻译助手
description: 将文本翻译为指定语言
command: python3 translate.py {input}
---

## 翻译技能

将用户输入翻译为目标语言。
```

`{input}` 会被安全转义后替换为用户输入。

### 12.3 MCP 服务器

配置 Model Context Protocol 服务器：

```yaml
skills:
  mcp_servers:
    - name: "filesystem"
      command: "npx"
      args: ["-y", "@modelcontextprotocol/server-filesystem", "./data/workspace"]
    - name: "github"
      command: "npx"
      args: ["-y", "@modelcontextprotocol/server-github"]
      env:
        GITHUB_TOKEN: "${GITHUB_TOKEN}"
```

---

## 13. 执行环境（Shell / Docker / 浏览器）

### 13.1 Shell 执行器

本地命令执行，带正则白名单安全校验：

```yaml
execution:
  local_shell:
    enabled: true
    default_timeout: 60
```

允许的命令模式（正则匹配）：

| 命令 | 允许的操作 |
|------|----------|
| `python3` | 运行 .py 脚本 |
| `node` | 运行 .js 脚本 |
| `git` | clone/pull/status/log 等只读操作 |
| `curl` | GET 请求 |
| `ls`/`cat`/`grep`/`find` | 文件查看 |
| `echo`/`date`/`uptime` | 系统信息 |

被禁止的命令：`rm`、`sudo`、`chmod`、`curl POST`、`git push --force` 等。

### 13.2 Docker 沙箱

更安全的隔离执行环境：

```yaml
execution:
  docker:
    enabled: true
    image: "python:3.12-slim"
    memory_limit_mb: 512
    timeout: 120
```

Docker 安全加固：
- `--network=none` — 无网络访问
- `--read-only` — 只读文件系统
- `--user=1000:1000` — 非 root 用户
- `--cap-drop=all` — 丢弃所有 Linux 能力
- `--pids-limit=64` — 限制进程数
- `--security-opt=no-new-privileges` — 禁止提权

### 13.3 浏览器执行器

Headless 网页抓取：

```yaml
execution:
  browser:
    enabled: true
```

---

## 14. 定时任务调度

基于 APScheduler 的 cron 调度，让 Agent 主动执行任务。

### 14.1 内置定时任务

```yaml
scheduler:
  enabled: true
  builtin_jobs:
    - name: "memory_housekeeping"
      cron: "0 3 * * *"      # 每天凌晨 3 点
      enabled: true
    - name: "skill_pattern_mining"
      cron: "0 4 * * 0"      # 每周日凌晨 4 点
      enabled: true
    - name: "router_statistics"
      cron: "*/30 * * * *"   # 每 30 分钟
      enabled: true
```

Cron 表达式格式：`分 时 日 月 星期`

### 14.2 自定义任务

在 `data/scheduler/jobs.yaml` 中添加：

```yaml
- name: "daily_report"
  cron: "0 9 * * 1-5"   # 工作日每天 9 点
  enabled: true
```

---

## 15. 监控仪表盘

浏览器访问 **http://127.0.0.1:18793/**

实时显示：

- **Event Bus**：发布/处理事件数、队列深度、事件/秒、死信队列
- **LLM**：调用次数、Token 用量、总费用、缓存命中率
- **Memory**：长期记忆条数、平均权重、程序技能数
- **Dead-Letter Queue**：未处理事件列表
- **Recent Logs**：最近 50 行日志

自动每 5 秒刷新。

---

## 16. 多模态功能（图像/语音）

### 16.1 启用

```yaml
multimodal:
  enabled: true
  image_model: "openai/dall-e-3"     # 图像生成
  vision_model: "openai/gpt-4o"      # 图像理解
  tts_model: "openai/tts-1"          # 文字转语音
```

需要对应的 API Key（如 OpenAI）。

### 16.2 通过 API 使用

```python
# 图像生成
result = await app.multimodal.generate_image(
    prompt="一只在月球上弹吉他的猫",
    model="openai/dall-e-3",
    size="1024x1024",
)

# 图像理解
result = await app.multimodal.analyze_image(
    image_data="/path/to/image.png",  # 本地路径 / URL / base64
    prompt="描述这张图片",
)

# 文字转语音
result = await app.multimodal.text_to_speech(
    text="你好，世界",
    voice="alloy",
)
```

---

## 17. 插件市场

### 17.1 浏览可用技能

```python
skills = await app.marketplace.browse_registry(query="翻译")
```

### 17.2 安装技能

```python
# 从 GitHub 仓库安装
result = await app.marketplace.install("owner/repo/skills/translate.md")

# 从 URL 安装
result = await app.marketplace.install(
    "https://raw.githubusercontent.com/owner/repo/main/skills/translate.md"
)
```

### 17.3 卸载技能

```python
result = await app.marketplace.uninstall("translate")
```

### 17.4 列出已安装技能

```python
installed = app.marketplace.list_installed()
```

---

## 18. 数据加密

Athena 支持用 Fernet 对称加密保护 API Key。

### 18.1 生成加密密钥

```python
from cryptography.fernet import Fernet
key = Fernet.generate_key()
print(key.decode())
```

### 18.2 加密 API Key

```python
from cryptography.fernet import Fernet
cipher = Fernet(b"你的密钥")
encrypted = cipher.encrypt(b"sk-your-api-key")
print("enc:" + encrypted.decode())
```

### 18.3 配置

```bash
export ATHENA_ENCRYPTION_KEY="你的Fernet密钥"
```

在配置文件中使用加密后的值：

```yaml
llm:
  api_keys:
    openrouter: "enc:gAAAAABm..."
```

---

## 19. 冒烟测试

零网络依赖的端到端测试，验证核心链路：

```bash
python tests/smoke.py
```

测试覆盖：

| 测试项 | 验证内容 |
|--------|---------|
| imports | 所有模块可正常导入 |
| LLM cache | LRU 缓存读写 + TTL 过期 + 淘汰 |
| event bus DLQ | 死信队列 + 事件追踪 + 指标 |
| shell patterns | 正则白名单允许/拒绝 |
| memory pagination | FTS5 分页查询 |
| plugin discovery | 自动发现插件 |
| coordinator pipeline | 完整链路：路由 → LLM → 回复 |

预期输出：

```
=== smoke test v2 ===
  all imports OK
  LLM cache OK
  event bus DLQ OK
  shell executor patterns OK
  memory pagination OK
  plugin discovery OK
  coordinator pipeline OK
  ✓ imports
  ✓ LLM cache
  ✓ event bus DLQ
  ✓ shell executor patterns
  ✓ memory pagination
  ✓ plugin discovery
  ✓ coordinator pipeline

7/7 tests passed
```

---

## 20. 常见问题

### Q: 启动后提示 "no API key configured"

确保至少配置了一个 LLM 提供商的 API Key：

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
```

或在 `config/default_config.yaml` 的 `llm.api_keys` 中直接填写。

### Q: Web UI 打不开

1. 检查是否安装了 `fastapi` 和 `uvicorn`：`pip install fastapi uvicorn`
2. 检查端口是否被占用：`lsof -i :18791`
3. 修改端口：在配置文件中修改 `gateways.web.port`

### Q: Telegram Bot 不回复

1. 确认 `gateways.telegram.enabled` 设为 `true`
2. 确认 `bot_token` 正确
3. 查看日志：`cat data/logs/athena.log | grep telegram`

### Q: 如何使用本地模型（Ollama）

1. 安装并启动 Ollama：`ollama serve`
2. 拉取模型：`ollama pull qwen2.5:7b`
3. 配置：

```yaml
llm:
  api_keys:
    ollama: "ollama"   # Ollama 不需要真实 Key，填任意值即可
  primary_model: "qwen/qwen-2.5-7b-instruct"
```

Ollama 默认地址 `http://localhost:11434` 已内置，无需额外配置。

### Q: 如何降低 Token 消耗

1. 开启上下文压缩（默认已开启）
2. 调低路由阈值，让更多问题走轻量模型：

```yaml
router:
  task_complexity_thresholds:
    trivial: 0.3    # 更多问题走 trivial
    simple: 0.6
    complex: 0.9
```

3. 开启 LLM 缓存（默认已开启）
4. 减少每轮加载的技能数：

```yaml
skills:
  max_skills_per_turn: 3
```

### Q: 数据存储在哪里

所有本地数据默认在 `./data/` 目录下：

```
data/
├── memory/
│   ├── longterm.sqlite    # FTS5 长期记忆数据库
│   └── skills/            # 自动生成的程序记忆
├── skills/
│   ├── builtin/           # 内置技能
│   ├── user/              # 用户自定义技能
│   └── community/         # 社区安装的技能
├── workspace/             # 执行器工作目录
├── logs/
│   ├── athena.log         # 主日志
│   └── executor_audit.log # 执行审计日志
├── marketplace/
│   └── registry.json      # 插件市场注册表
└── scheduler/
    └── jobs.yaml          # 自定义定时任务
```

该目录已在 `.gitignore` 中排除，不会被提交到 Git。

### Q: 如何在生产环境部署

1. 设置 API Key 认证：

```bash
export ATHENA_API_KEY="强密码"
```

2. 启用数据加密：

```bash
export ATHENA_ENCRYPTION_KEY="Fernet密钥"
```

3. 使用 Docker 执行器替代 Shell：

```yaml
execution:
  local_shell:
    enabled: false
  docker:
    enabled: true
```

4. 限制 Web UI 访问地址：

```yaml
gateways:
  web:
    host: "127.0.0.1"   # 仅本地访问
```

5. 使用 systemd 或 supervisor 管理进程。

---

## 架构速览

```
┌─────────────── 微内核 EventBus ───────────────┐
│                                                │
│  gateways ─► user_message ─► router ─► LLM    │
│  (CLI/TG/Web)       │                   │     │
│                      ▼                   ▼     │
│                  memory 3-tier    skills+exec   │
│                (short/long/proc) (MCP/docker)  │
│                                                │
└──────────── scheduler (cron) ──────────────────┘
```

**核心设计原则：**
- **零魔法** — 每个模块 < 300 行，事件总线通信，无黑盒框架
- **成本优先** — 按难度切模型，按需加载技能，自动压缩上下文
- **可插拔** — 任何子系统可独立替换
- **本地第一** — 所有状态写入本地 `data/`，可审计、可移植

---

> GitHub: https://github.com/huang2025511/agnet
> License: MIT
