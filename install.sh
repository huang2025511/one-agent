#!/usr/bin/env bash
# ============================================================
# One-Agent 安装脚本
# 支持语言：中文(zh) / English(en)  |  默认：中文
# 用法：bash install.sh          # 中文
#       bash install.sh en       # 英文
#       LANG=en bash install.sh  # 英文（环境变量方式）
#
# 国内网络优化：
#   - 自动检测是否在中国大陆，优先使用清华/阿里云 PyPI 镜像
#   - 也可手动指定: PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple bash install.sh
# ============================================================

set -e

# ---------- 语言选择 ----------
LANG_CODE="${1:-${LANG:-zh}}"
if [[ "$LANG_CODE" != "en" ]]; then
    LANG_CODE="zh"
fi

# ---------- 国内镜像源检测 ----------
# 优先级: 环境变量 > 自动检测 > 官方源
_detect_china_network() {
    # 如果用户已显式指定 PIP_INDEX，直接使用
    if [[ -n "${PIP_INDEX:-}" ]]; then
        echo "$PIP_INDEX"
        return
    fi
    # 检查是否在中国大陆：通过连接 pypi.org 的超时时间判断
    # 国内直连 pypi.org 通常 > 2s 或超时，用清华源更快
    if command -v curl &>/dev/null; then
        local elapsed
        elapsed=$(curl -s -o /dev/null -w '%{time_total}' --connect-timeout 2 https://pypi.org/simple/ 2>/dev/null || echo "5")
        if [[ "$(echo "$elapsed > 1.5" | bc 2>/dev/null || echo 1)" == "1" ]]; then
            echo "https://pypi.tuna.tsinghua.edu.cn/simple"
            return
        fi
    fi
    # 检查系统语言环境辅助判断
    if [[ "$LANG" =~ zh_CN ]] || [[ "$(locale 2>/dev/null | grep -i cn)" ]]; then
        echo "https://pypi.tuna.tsinghua.edu.cn/simple"
        return
    fi
    echo ""
}

PIP_MIRROR=$(_detect_china_network)
_pip_install() {
    if [[ -n "$PIP_MIRROR" ]]; then
        $PY -m pip install --quiet -i "$PIP_MIRROR" --trusted-host pypi.tuna.tsinghua.edu.cn "$@"
    else
        $PY -m pip install --quiet "$@"
    fi
}

# 配置 pip 全局镜像（持久化，方便后续安装）
_configure_pip_mirror() {
    if [[ -z "$PIP_MIRROR" ]]; then
        return
    fi
    local pip_conf
    if [[ -n "${VIRTUAL_ENV:-}" ]]; then
        pip_conf="$VIRTUAL_ENV/pip.conf"
    elif [[ -d "$HOME/.config/pip" ]]; then
        pip_conf="$HOME/.config/pip/pip.conf"
    else
        mkdir -p "$HOME/.config/pip" 2>/dev/null || true
        pip_conf="$HOME/.config/pip/pip.conf"
    fi
    cat > "$pip_conf" << PIPEOF
[global]
index-url = $PIP_MIRROR
trusted-host = ${PIP_MIRROR#https://}
PIPEOF
    if [[ "$LANG_CODE" == "zh" ]]; then
        printf "  pip 镜像已配置: %s\n" "$PIP_MIRROR"
    else
        printf "  pip mirror configured: %s\n" "$PIP_MIRROR"
    fi
}

# ---------- 多语言文本 ----------
declare -A T
if [[ "$LANG_CODE" == "zh" ]]; then
    T[welcome]="\n  ╔══════════════════════════════════════╗"
    T[welcome2]="  ║   One-Agent v2.0 安装向导        ║"
    T[welcome3]="  ╚══════════════════════════════════════╝\n"
    T[lang]="语言: 中文"
    T[check_py]="[1/7] 检查 Python 环境..."
    T[py_fail]="未找到 Python 3.10+，请安装后重试。"
    T[py_ok]="Python 版本: "
    T[ask_venv]="[2/7] 是否创建虚拟环境？[Y/n] "
    T[venv_create]="正在创建虚拟环境..."
    T[venv_activate]="虚拟环境已创建。后续使用请先执行: source venv/bin/activate"
    T[venv_skip]="跳过虚拟环境创建。"
    T[ask_deps]="[3/7] 选择安装模式:\n  1) 最小安装 (仅 CLI + LLM)\n  2) 完整安装 (Web UI + 调度器 + 全部功能)\n请选择 [1/2, 默认2]: "
    T[install_min]="正在安装最小依赖..."
    T[install_full]="正在安装完整依赖..."
    T[install_done]="依赖安装完成。"
    T[ask_key]="[5/7] 配置 LLM API Key（至少配一个，也可跳过后手动配置）"
    T[ask_provider]="选择提供商: 1)OpenRouter 2)OpenAI 3)Anthropic 4)DeepSeek 5)跳过 [1-5, 默认1]: "
    T[enter_key]="请输入 API Key: "
    T[key_saved]="API Key 已保存到 .env 文件。"
    T[key_skip]="跳过 API Key 配置。稍后可编辑 .env 或 config/default_config.yaml。"
    T[ask_lang]="[6/7] 选择 Agent 界面语言:\n  1) 中文 (Asia/Shanghai)\n  2) English (UTC)\n请选择 [1/2, 默认1]: "
    T[lang_cn]="界面语言设为中文，时区 Asia/Shanghai。"
    T[lang_en]="Interface language set to English, timezone UTC."
    T[init_data]="[7/7] 初始化数据目录..."
    T[data_done]="数据目录已就绪。"
    T[done]="\n✅ 安装完成！\n\n启动方式:\n  one-agent                 # 直接输入即可启动\n  one                       # 简写别名\n\n如果使用虚拟环境，先执行:\n  source venv/bin/activate\n\n服务地址:\n  Web UI:    http://127.0.0.1:18791\n  REST API:  http://127.0.0.1:18792\n  监控面板:  http://127.0.0.1:18793\n\n详细教程: cat TUTORIAL.md\n"
    T[smoke]="运行冒烟测试验证安装: python tests/smoke.py\n"
else
    T[welcome]="\n  ╔══════════════════════════════════════╗"
    T[welcome2]="  ║   One-Agent v2.0 Setup Wizard    ║"
    T[welcome3]="  ╚══════════════════════════════════════╝\n"
    T[lang]="Language: English"
    T[check_py]="[1/7] Checking Python..."
    T[py_fail]="Python 3.10+ not found. Please install it and retry."
    T[py_ok]="Python version: "
    T[ask_venv]="[2/7] Create a virtual environment? [Y/n] "
    T[venv_create]="Creating virtual environment..."
    T[venv_activate]="Virtual environment created. Activate it with: source venv/bin/activate"
    T[venv_skip]="Skipping virtual environment."
    T[ask_deps]="[3/7] Installation mode:\n  1) Minimal (CLI + LLM only)\n  2) Full (Web UI + scheduler + all features)\nChoose [1/2, default 2]: "
    T[install_min]="Installing minimal dependencies..."
    T[install_full]="Installing full dependencies..."
    T[install_done]="Dependencies installed."
    T[ask_key]="[5/7] Configure LLM API Key (at least one, or skip to configure later)"
    T[ask_provider]="Choose provider: 1)OpenRouter 2)OpenAI 3)Anthropic 4)DeepSeek 5)Skip [1-5, default 1]: "
    T[enter_key]="Enter API Key: "
    T[key_saved]="API Key saved to .env file."
    T[key_skip]="Skipping API Key setup. You can edit .env or config/default_config.yaml later."
    T[ask_lang]="[6/7] Choose agent interface language:\n  1) 中文 (Asia/Shanghai)\n  2) English (UTC)\nChoose [1/2, default 2]: "
    T[lang_cn]="界面语言设为中文，时区 Asia/Shanghai。"
    T[lang_en]="Interface language set to English, timezone UTC."
    T[init_data]="[7/7] Initializing data directories..."
    T[data_done]="Data directories ready."
    T[done]="\n✅ Installation complete!\n\nTo start:\n  one-agent                 # run directly\n  one                       # short alias\n\nIf using virtual environment, activate first:\n  source venv/bin/activate\n\nServices:\n  Web UI:    http://127.0.0.1:18791\n  REST API:  http://127.0.0.1:18792\n  Monitor:   http://127.0.0.1:18793\n\nFull tutorial: cat TUTORIAL.md\n"
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

# ---- [3/7] 安装依赖 ----
ans=$(prompt "${T[ask_deps]}")
if [[ "$ans" == "1" ]]; then
    printf "%b" "${T[install_min]}\n"
    _pip_install --upgrade pip
    _pip_install pyyaml httpx pydantic
else
    printf "%b" "${T[install_full]}\n"
    _pip_install --upgrade pip
    _pip_install -r requirements.txt
fi
# 持久化配置 pip 镜像，方便后续手工安装依赖
_configure_pip_mirror
printf "%b" "${T[install_done]}\n"

# ---- [4/7] 注册 CLI 命令 (one-agent / one) ----
if [[ "$LANG_CODE" == "zh" ]]; then
    printf "\n[4/7] 注册 one-agent 命令...\n"
else
    printf "\n[4/7] Registering one-agent command...\n"
fi
# 方案 A：通过 pip install -e . 注册（推荐，使用 pyproject.toml 的 console_scripts）
_pip_install --upgrade pip setuptools wheel 2>/dev/null || true
if $PY -m pip install -e . 2>/dev/null; then
    if [[ "$LANG_CODE" == "zh" ]]; then
        printf "  ✓ one-agent 命令已注册，可直接在终端输入 one-agent 启动\n"
    else
        printf "  ✓ one-agent command registered. Run: one-agent\n"
    fi
else
    # 方案 B：兜底 - 创建 wrapper 脚本到 /usr/local/bin
    if [[ "$LANG_CODE" == "zh" ]]; then
        printf "  ⚠ pip 注册失败，尝试创建系统级 wrapper 脚本...\n"
    else
        printf "  ⚠ pip registration failed, creating system wrapper...\n"
    fi
    SCRIPT_DIR="$(pwd)"
    for cmd_name in one-agent one; do
        if [[ -w /usr/local/bin ]] || [[ "$(id -u)" == "0" ]]; then
            cat > "/usr/local/bin/${cmd_name}" << WRAPEOF
#!/usr/bin/env bash
cd "${SCRIPT_DIR}" && exec $PY one_agent.py "\$@"
WRAPEOF
            chmod +x "/usr/local/bin/${cmd_name}"
            if [[ "$LANG_CODE" == "zh" ]]; then
                printf "  ✓ 已创建: /usr/local/bin/%s\n" "$cmd_name"
            else
                printf "  ✓ Created: /usr/local/bin/%s\n" "$cmd_name"
            fi
        else
            if [[ "$LANG_CODE" == "zh" ]]; then
                printf "  ⚠ 无权限写入 /usr/local/bin，请用 sudo 重试安装或手动创建别名:\n"
                printf "    alias one='cd %s && %s one_agent.py'\n" "$SCRIPT_DIR" "$PY"
            else
                printf "  ⚠ Cannot write to /usr/local/bin. Run with sudo or create alias manually:\n"
                printf "    alias one='cd %s && %s one_agent.py'\n" "$SCRIPT_DIR" "$PY"
            fi
        fi
    done
fi

# ---- [5/7] API Key ----
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

# ---- [6/7] 界面语言 ----
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

# ---- [7/7] 数据目录 ----
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
