#!/usr/bin/env bash
# ============================================================
# One-Agent 安装脚本
# 支持语言：中文(zh) / English(en)  |  默认：中文
# 用法：bash install.sh          # 中文
#       bash install.sh en       # 英文
#       LANG=en bash install.sh  # 英文（环境变量方式）
# ============================================================

set -e

# ---------- 语言选择 ----------
LANG_CODE="${1:-${LANG:-zh}}"
if [[ "$LANG_CODE" != "en" ]]; then
    LANG_CODE="zh"
fi

# ---------- 多语言文本 ----------
declare -A T
if [[ "$LANG_CODE" == "zh" ]]; then
    T[welcome]="\n  ╔══════════════════════════════════════╗"
    T[welcome2]="  ║   One-Agent v2.0 安装向导        ║"
    T[welcome3]="  ╚══════════════════════════════════════╝\n"
    T[lang]="语言: 中文"
    T[check_py]="[1/6] 检查 Python 环境..."
    T[py_fail]="未找到 Python 3.10+，请安装后重试。"
    T[py_ok]="Python 版本: "
    T[ask_venv]="[2/6] 是否创建虚拟环境？[Y/n] "
    T[venv_create]="正在创建虚拟环境..."
    T[venv_activate]="虚拟环境已创建。后续使用请先执行: source venv/bin/activate"
    T[venv_skip]="跳过虚拟环境创建。"
    T[ask_deps]="[3/6] 选择安装模式:\n  1) 最小安装 (仅 CLI + LLM)\n  2) 完整安装 (Web UI + 调度器 + 全部功能)\n请选择 [1/2, 默认2]: "
    T[install_min]="正在安装最小依赖..."
    T[install_full]="正在安装完整依赖..."
    T[install_done]="依赖安装完成。"
    T[ask_key]="[4/6] 配置 LLM API Key（至少配一个，也可跳过后手动配置）"
    T[ask_provider]="选择提供商: 1)OpenRouter 2)OpenAI 3)Anthropic 4)DeepSeek 5)跳过 [1-5, 默认1]: "
    T[enter_key]="请输入 API Key: "
    T[key_saved]="API Key 已保存到 .env 文件。"
    T[key_skip]="跳过 API Key 配置。稍后可编辑 .env 或 config/default_config.yaml。"
    T[ask_lang]="[5/6] 选择 Agent 界面语言:\n  1) 中文 (Asia/Shanghai)\n  2) English (UTC)\n请选择 [1/2, 默认1]: "
    T[lang_cn]="界面语言设为中文，时区 Asia/Shanghai。"
    T[lang_en]="Interface language set to English, timezone UTC."
    T[init_data]="[6/6] 初始化数据目录..."
    T[data_done]="数据目录已就绪。"
    T[done]="\n✅ 安装完成！\n\n启动方式:\n  source venv/bin/activate   # 如果使用了虚拟环境\n  python one_agent.py\n\n服务地址:\n  Web UI:    http://127.0.0.1:18791\n  REST API:  http://127.0.0.1:18792\n  监控面板:  http://127.0.0.1:18793\n\n详细教程: cat TUTORIAL.md\n"
    T[smoke]="运行冒烟测试验证安装: python tests/smoke.py\n"
else
    T[welcome]="\n  ╔══════════════════════════════════════╗"
    T[welcome2]="  ║   One-Agent v2.0 Setup Wizard    ║"
    T[welcome3]="  ╚══════════════════════════════════════╝\n"
    T[lang]="Language: English"
    T[check_py]="[1/6] Checking Python..."
    T[py_fail]="Python 3.10+ not found. Please install it and retry."
    T[py_ok]="Python version: "
    T[ask_venv]="[2/6] Create a virtual environment? [Y/n] "
    T[venv_create]="Creating virtual environment..."
    T[venv_activate]="Virtual environment created. Activate it with: source venv/bin/activate"
    T[venv_skip]="Skipping virtual environment."
    T[ask_deps]="[3/6] Installation mode:\n  1) Minimal (CLI + LLM only)\n  2) Full (Web UI + scheduler + all features)\nChoose [1/2, default 2]: "
    T[install_min]="Installing minimal dependencies..."
    T[install_full]="Installing full dependencies..."
    T[install_done]="Dependencies installed."
    T[ask_key]="[4/6] Configure LLM API Key (at least one, or skip to configure later)"
    T[ask_provider]="Choose provider: 1)OpenRouter 2)OpenAI 3)Anthropic 4)DeepSeek 5)Skip [1-5, default 1]: "
    T[enter_key]="Enter API Key: "
    T[key_saved]="API Key saved to .env file."
    T[key_skip]="Skipping API Key setup. You can edit .env or config/default_config.yaml later."
    T[ask_lang]="[5/6] Choose agent interface language:\n  1) 中文 (Asia/Shanghai)\n  2) English (UTC)\nChoose [1/2, default 2]: "
    T[lang_cn]="界面语言设为中文，时区 Asia/Shanghai。"
    T[lang_en]="Interface language set to English, timezone UTC."
    T[init_data]="[6/6] Initializing data directories..."
    T[data_done]="Data directories ready."
    T[done]="\n✅ Installation complete!\n\nTo start:\n  source venv/bin/activate   # if using virtual environment\n  python one_agent.py\n\nServices:\n  Web UI:    http://127.0.0.1:18791\n  REST API:  http://127.0.0.1:18792\n  Monitor:   http://127.0.0.1:18793\n\nFull tutorial: cat TUTORIAL.md\n"
    T[smoke]="Run smoke test to verify: python tests/smoke.py\n"
fi

# ---------- 工具函数 ----------
prompt() { printf "%b" "$1"; read -r ans; echo "$ans"; }

# ---------- 开始 ----------
printf "%b" "${T[welcome]}\n${T[welcome2]}\n${T[welcome3]}\n${T[lang]}\n"

# 切换到脚本所在目录
cd "$(dirname "$0")"

# ---- [1/6] Python 检查 ----
printf "%b" "${T[check_py]}\n"
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    printf "%b" "${T[py_fail]}\n"; exit 1
fi
PY_VER=$($PY -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [[ "$PY_MAJOR" -lt 3 ]] || [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 10 ]]; then
    printf "%b" "${T[py_fail]}\n"; exit 1
fi
printf "%b" "${T[py_ok]}${PY_VER}\n"

# ---- [2/6] 虚拟环境 ----
ans=$(prompt "${T[ask_venv]}")
if [[ -z "$ans" || "$ans" =~ ^[Yy] ]]; then
    printf "%b" "${T[venv_create]}\n"
    $PY -m venv venv
    source venv/bin/activate
    PY=python
    printf "%b" "${T[venv_activate]}\n"
else
    printf "%b" "${T[venv_skip]}\n"
fi

# ---- [3/6] 安装依赖 ----
ans=$(prompt "${T[ask_deps]}")
if [[ "$ans" == "1" ]]; then
    printf "%b" "${T[install_min]}\n"
    $PY -m pip install --quiet --upgrade pip
    $PY -m pip install --quiet pyyaml httpx
else
    printf "%b" "${T[install_full]}\n"
    $PY -m pip install --quiet --upgrade pip
    $PY -m pip install --quiet -r requirements.txt
fi
printf "%b" "${T[install_done]}\n"

# ---- [4/6] API Key ----
printf "%b" "${T[ask_key]}\n"
ans=$(prompt "${T[ask_provider]}")
case "${ans:-1}" in
    1) PROVIDER="OPENROUTER_API_KEY";;
    2) PROVIDER="OPENAI_API_KEY";;
    3) PROVIDER="ANTHROPIC_API_KEY";;
    4) PROVIDER="DEEPSEEK_API_KEY";;
    *) PROVIDER="";;
esac

if [[ -n "$PROVIDER" ]]; then
    KEY=$(prompt "${T[enter_key]}")
    if [[ -n "$KEY" ]]; then
        echo "${PROVIDER}=${KEY}" >> .env
        printf "%b" "${T[key_saved]}\n"
    else
        printf "%b" "${T[key_skip]}\n"
    fi
else
    printf "%b" "${T[key_skip]}\n"
fi

# ---- [5/6] 界面语言 ----
ans=$(prompt "${T[ask_lang]}")
if [[ "$ans" == "1" ]]; then
    TZ="Asia/Shanghai"
    printf "%b" "${T[lang_cn]}\n"
else
    TZ="UTC"
    printf "%b" "${T[lang_en]}\n"
fi
# 写入配置
if command -v sed &>/dev/null; then
    sed -i.bak "s|timezone: .*|timezone: \"${TZ}\"|" config/default_config.yaml 2>/dev/null || true
    rm -f config/default_config.yaml.bak
fi

# ---- [6/6] 数据目录 ----
printf "%b" "${T[init_data]}\n"
mkdir -p data/skills/builtin data/skills/user data/skills/community
mkdir -p data/memory/skills data/workspace data/logs
mkdir -p data/marketplace data/scheduler
printf "%b" "${T[data_done]}\n"

# ---- [7/7] 安全密码（可选）----
printf "\n  是否设置系统命令执行密码？\n"
printf "  设置后，one-agent 执行 rm、sudo 等危险命令时需要输入密码确认。\n"
printf "  留空跳过：不使用密码保护（仅安全命令可执行）。\n"
SYS_PWD=$(prompt "  系统执行密码 [留空跳过]: ")
if [[ -n "$SYS_PWD" ]]; then
    # Generate SHA-256 hash
    if command -v python3 &>/dev/null; then
        PWD_HASH=$(python3 -c "import hashlib; print(hashlib.sha256('${SYS_PWD}'.encode()).hexdigest())")
        # Update config
        sed -i.bak "s|system_executor_password: ''|system_executor_password: '${PWD_HASH}'|" config/default_config.yaml 2>/dev/null || true
        rm -f config/default_config.yaml.bak
        printf "  ✓ 密码已设置（SHA-256 哈希存储）\n"
    else
        printf "  ⚠ Python 不可用，无法哈希密码。请稍后手动设置。\n"
    fi
else
    printf "  已跳过。安全命令（ls/cat/echo 等）无需密码即可执行。\n"
fi

# ---------- 完成 ----------
printf "%b" "${T[done]}${T[smoke]}"
