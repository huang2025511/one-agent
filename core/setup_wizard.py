"""Setup wizard — auto-detects missing or invalid configuration and guides
the user through first-time setup via CLI or Web UI.

This runs automatically when:
  - No API key is configured for any LLM provider
  - No ``.env`` file exists
  - Config file is missing or empty
  - The agent replies with error messages (detected via hook)

Architecture:
  ``run_setup_wizard_mode()`` is the entry point.  It returns True
  if setup was necessary (and the agent should exit/restart), False
  otherwise.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ---- Known API key env vars across all providers ----
_KNOWN_KEY_ENV_VARS: List[str] = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "SENSENOVA_API_KEY",
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "ZHIPU_API_KEY",
    "MOONSHOT_API_KEY",
    "BAICHUAN_API_KEY",
    "MINIMAX_API_KEY",
    "OPENROUTER_API_KEY",
    "OLLAMA_HOST",  # local; no key needed
    "ONE_AGENT_API_KEY",  # not an LLM key but indicates setup was done
]

# ---- Providers available without key (local/free-tier) ----
_NO_KEY_PROVIDERS: Set[str] = {"ollama", "local", "lmstudio"}

# ---- WeChat Personal dependencies ----
_WECHAT_PERSONAL_DEPS: Dict[str, str] = {
    "itchat-uos": "itchat-uos>=1.5.0",
}


def _has_any_api_key() -> bool:
    """Check if at least one known API key env var is set and non-empty."""
    for var in _KNOWN_KEY_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val and len(val) > 3:
            return True
    # Also check .env file
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key_part = line.split("=", 1)[0].strip()
                val_part = line.split("=", 1)[1].strip()
                if key_part in _KNOWN_KEY_ENV_VARS and val_part and val_part != f"YOUR_{key_part}":
                    return True
    return False


def _find_config_path() -> Optional[Path]:
    """Locate the config file.  Returns None if not found or empty."""
    for candidate in [
        Path(os.environ.get("ONE_AGENT_CONFIG", "")),
        Path("config/default_config.yaml"),
        Path("config/config.yaml"),
        Path("one_agent_config.yaml"),
    ]:
        if candidate.exists() and candidate.stat().st_size > 10:
            return candidate
    return None


def _prompt(prompt_text: str, default: str = "") -> str:
    """Ask a CLI question, return user's answer."""
    try:
        ans = input(prompt_text)
    except (EOFError, KeyboardInterrupt):
        return default
    return ans.strip() or default


# ============================================================
#  CLI Setup Wizard
# ============================================================
def run_cli_setup() -> bool:
    """Run guided CLI setup.  Returns True if user completed setup."""
    print()
    print("  ╔══════════════════════════════════════════════════╗")
    print("  ║        One-Agent 首次设置向导                   ║")
    print("  ║        First-Time Setup Wizard                   ║")
    print("  ╚══════════════════════════════════════════════════╝")
    print()
    print("  检测到未配置 API Key。没有 API Key 时 Agent 无法调用 LLM。")
    print("  No API key detected. The agent needs one to work.")
    print()
    print("  可用的免费/低成本提供商 (free / low-cost providers):")
    print()
    print("    1. SenseNova (商汤科技)   — 新用户有免费额度")
    print("       https://platform.sensenova.com")
    print("       注册后可获取 API Key，格式: sk-xxx")
    print()
    print("    2. DeepSeek (深度求索)    — 极低价格")
    print("       https://platform.deepseek.com")
    print("       注册后可获取 API Key")
    print()
    print("    3. DashScope (阿里百炼)   — 新用户有免费额度")
    print("       https://dashscope.console.aliyun.com")
    print("       注册后可获取 API Key，格式: sk-xxx")
    print()
    print("    4. OpenAI                 — 按用量付费")
    print("       https://platform.openai.com")
    print()
    print("    5. Anthropic              — 按用量付费")
    print("       https://console.anthropic.com")
    print()
    print("    6. Ollama (本地/免费)     — 需先安装 Ollama")
    print("       https://ollama.com")
    print("       安装后无需 API Key，自动可用")
    print()
    print("    7. 稍后设置 (Skip for now)")
    print()

    choice = _prompt("  请选择 [1-7, 默认 1]: ", "1")

    provider_map: Dict[str, Tuple[str, str, str, List[str]]] = {
        "1": ("SENSENOVA_API_KEY", "sensenova", "SenseNova", "sk-",
              ["deepseek-v4-flash", "sensenova-default", "deepseek-v4"]),
        "2": ("DEEPSEEK_API_KEY", "deepseek", "DeepSeek", "sk-",
              ["deepseek-chat", "deepseek-reasoner"]),
        "3": ("DASHSCOPE_API_KEY", "dashscope", "DashScope", "sk-",
              ["qwen-plus", "qwen-max", "qwen-turbo"]),
        "4": ("OPENAI_API_KEY", "openai", "OpenAI", "sk-",
              ["gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo"]),
        "5": ("ANTHROPIC_API_KEY", "anthropic", "Anthropic", "sk-",
              ["claude-sonnet-4-20250514", "claude-3-5-haiku-20241022"]),
        "6": ("OLLAMA_HOST", "ollama", "Ollama", "http://localhost:11434",
              ["qwen2.5:7b", "llama3", "deepseek-r1:7b"]),
        "7": (None, None, None, None, []),
    }

    info = provider_map.get(choice)
    if info is None or info[0] is None:
        print("\n  已跳过 API Key 设置。")
        print("  稍后可编辑 .env 文件添加 API Key，或重新运行 one-agent。")
        print("  (Skipped. Edit .env file later or re-run one-agent.)\n")
        return False

    env_var, provider_key, name, suggested, models = info
    print(f"\n  {name} API Key 设置")
    if env_var == "OLLAMA_HOST":
        ans = _prompt(f"  Ollama 地址 [默认 {suggested}]: ", suggested)
        print(f"\n  ✓ {env_var}={ans}")
    else:
        ans = _prompt(f"  请输入 {name} API Key [{suggested}...]: ", "")
        if not ans:
            print("\n  未输入 API Key，已跳过。\n")
            return False
        if ans.startswith("sk-") is False and name != "DeepSeek":
            print(f"  ⚠  格式可能不正确 (期望 {suggested}...)。仍会保存。")
        print(f"\n  ✓ {env_var}={ans[:8]}...")

    # Write to .env
    _write_env(env_var, ans)

    # ---- 选择模型 ----
    print(f"\n  可用的 {name} 模型:")
    for i, model in enumerate(models, 1):
        default_mark = " (默认)" if i == 1 else ""
        print(f"    {i}. {model}{default_mark}")
    print(f"    {len(models) + 1}. 自定义输入 (Custom)")
    model_choice = _prompt("  请选择模型 [默认 1]: ", "1")
    try:
        idx = int(model_choice) - 1
        if 0 <= idx < len(models):
            selected_model = models[idx]
        else:
            selected_model = _prompt("  请输入模型名称: ", models[0])
    except ValueError:
        selected_model = _prompt("  请输入模型名称: ", models[0])
    print(f"  ✓ 模型: {selected_model}")

    # Also write default config if missing
    _ensure_config(provider_key, selected_model)

    print("\n  ✅ 设置完成！正在启动 One-Agent...\n")
    return True


def _write_env(key: str, value: str) -> None:
    """Append or update an env var in .env file."""
    env_path = Path(".env")
    env_path.touch(exist_ok=True)
    # Read existing lines
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.stat().st_size > 0 else []
    updated = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _ensure_config(provider: str = "sensenova", model: str = "deepseek-v4-flash") -> None:
    """Ensure config/default_config.yaml exists with sensible defaults."""
    config_path = Path("config/default_config.yaml")
    if config_path.exists() and config_path.stat().st_size > 50:
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        _MINIMAL_CONFIG.format(provider=provider, model=f"{provider}/{model}"),
        encoding="utf-8",
    )


_MINIMAL_CONFIG = """\
# One-Agent 默认配置 — 首次运行自动生成
agent:
  name: One-Agent
  version: 2.0.0
  data_dir: ./data
  log_level: INFO
  language: zh

llm:
  primary_provider: {provider}
  primary_model: {model}
  default_temperature: 0.3
  default_max_tokens: 2048
  timeout: 60
  retries: 3

gateways:
  cli:
    enabled: true
    prompt: 'one-agent> '
  web:
    enabled: true
    host: 127.0.0.1
    port: 18791

router:
  task_complexity_thresholds: { trivial: 0.2, simple: 0.5, complex: 0.8, expert: 1.0 }

memory:
  short_term:
    max_turns: 20
    max_tokens: 8000

security:
  system_executor_password: ""
  require_password_for_dangerous: true
"""


# ============================================================
#  Auto-detect and trigger
# ============================================================
def setup_if_needed() -> bool:
    """Entry point: auto-detect missing config and run setup if needed.

    Returns True if setup was run (agent should restart), False otherwise.
    """
    if _has_any_api_key():
        return False

    print("\n  ⚠  One-Agent needs an LLM API key to function.")
    print("  ⚠  One-Agent 需要 LLM API Key 才能工作。\n")

    # Check if we're in an interactive terminal
    if not sys.stdin.isatty():
        print("  非交互环境，跳过设置向导。请设置环境变量:")
        print("  Non-interactive environment. Set any of these env vars:")
        for var in _KNOWN_KEY_ENV_VARS[:6]:
            print(f"    export {var}=sk-your-key-here")
        print()
        return False

    return run_cli_setup()


# ============================================================
#  Web Setup endpoints (FastAPI)
# ============================================================
def register_setup_endpoints(app) -> None:
    """Mount /setup endpoints on a FastAPI app for Web-based configuration."""
    try:
        from fastapi import Request
        from fastapi.responses import JSONResponse
    except ImportError:
        return

    @app.get("/setup/status")
    async def setup_status():
        has_key = _has_any_api_key()
        keys_found = [v for v in _KNOWN_KEY_ENV_VARS if os.environ.get(v, "").strip()]
        return {
            "needs_setup": not has_key,
            "keys_configured": keys_found,
            "available_providers": [
                {"id": "sensenova", "name": "SenseNova / 商汤科技", "free_tier": True, "url": "https://platform.sensenova.com"},
                {"id": "deepseek", "name": "DeepSeek / 深度求索", "free_tier": False, "url": "https://platform.deepseek.com", "cheap": True},
                {"id": "dashscope", "name": "DashScope / 阿里百炼", "free_tier": True, "url": "https://dashscope.console.aliyun.com"},
                {"id": "openai", "name": "OpenAI", "free_tier": False, "url": "https://platform.openai.com"},
                {"id": "anthropic", "name": "Anthropic", "free_tier": False, "url": "https://console.anthropic.com"},
                {"id": "ollama", "name": "Ollama / 本地免费", "free_tier": True, "url": "https://ollama.com"},
            ],
        }

    @app.post("/setup/configure")
    async def setup_configure(request: Request):
        # Security: only allow setup from localhost. This endpoint
        # writes API keys to .env and os.environ, so remote access
        # would allow an attacker to overwrite keys and redirect all
        # LLM traffic to a malicious endpoint.
        client_host = request.client.host if request.client else ""
        if client_host not in ("127.0.0.1", "::1", "localhost"):
            return JSONResponse(
                {"ok": False, "error": "setup endpoint only accessible from localhost"},
                status_code=403,
            )

        data = await request.json()
        provider = data.get("provider", "").lower()
        api_key = data.get("api_key", "").strip()
        if not provider or not api_key:
            return JSONResponse({"ok": False, "error": "provider and api_key required"}, status_code=400)
        env_var_map = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "sensenova": "SENSENOVA_API_KEY",
            "dashscope": "DASHSCOPE_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
            "zhipu": "ZHIPU_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "ollama": "OLLAMA_HOST",
        }
        # Strict whitelist — reject unknown providers to prevent
        # arbitrary environment variable injection.
        if provider not in env_var_map:
            return JSONResponse(
                {"ok": False, "error": f"unsupported provider: {provider}"},
                status_code=400,
            )
        env_var = env_var_map[provider]
        # Reject values containing newlines (would corrupt .env format)
        if "\n" in api_key or "\r" in api_key:
            return JSONResponse(
                {"ok": False, "error": "api_key must not contain newlines"},
                status_code=400,
            )
        _write_env(env_var, api_key)
        os.environ[env_var] = api_key
        _ensure_config()
        return {"ok": True, "provider": provider, "env_var": env_var}

    @app.get("/setup")
    async def setup_page():
        """Serve a simple setup HTML page."""
        return _SETUP_HTML


_SETUP_HTML: str = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>One-Agent 设置向导</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; background: #f5f5f5; }
.card { background: white; border-radius: 8px; padding: 30px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
h1 { font-size: 24px; margin: 0 0 8px; }
p.subtitle { color: #666; margin: 0 0 20px; }
label { display: block; margin: 12px 0 4px; font-weight: 600; }
select, input { width: 100%; padding: 8px 12px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; box-sizing: border-box; }
button { margin-top: 16px; padding: 10px 24px; background: #4f46e5; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }
button:hover { background: #4338ca; }
.status { margin-top: 12px; padding: 8px; border-radius: 4px; display: none; }
.status.ok { display: block; background: #dcfce7; color: #166534; }
.status.err { display: block; background: #fee2e2; color: #991b1b; }
</style>
</head>
<body>
<div class="card">
<h1>One-Agent 设置向导</h1>
<p class="subtitle">未检测到 API Key，请选择一个 LLM 提供商并填入 Key。</p>
<label>提供商</label>
<select id="provider">
<option value="sensenova">SenseNova / 商汤科技 (新用户免费额度)</option>
<option value="dashscope">DashScope / 阿里百炼 (新用户免费额度)</option>
<option value="deepseek">DeepSeek / 深度求索 (极低价格)</option>
<option value="openai">OpenAI</option>
<option value="anthropic">Anthropic</option>
<option value="ollama">Ollama / 本地免费</option>
</select>
<label>API Key</label>
<input id="apikey" type="password" placeholder="输入你的 API Key...">
<button onclick="save()">保存并启动</button>
<div class="status" id="status"></div>
</div>
<script>
async function save() {
  var p = document.getElementById('provider').value;
  var k = document.getElementById('apikey').value.trim();
  var s = document.getElementById('status');
  if (!k) { s.className = 'status err'; s.innerText = '请输入 API Key'; return; }
  try {
    var r = await fetch('/setup/configure', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider: p, api_key: k})
    });
    var d = await r.json();
    if (d.ok) {
      s.className = 'status ok';
      s.innerText = '✓ 设置完成！请重新启动 One-Agent。';
    } else {
      s.className = 'status err';
      s.innerText = '错误: ' + (d.error || '未知错误');
    }
  } catch(e) {
    s.className = 'status err';
    s.innerText = '网络错误: ' + e.message;
  }
}
</script>
</div>
</body>
</html>"""

__all__ = [
    "setup_if_needed",
    "run_cli_setup",
    "register_setup_endpoints",
    "_has_any_api_key",
    "_KNOWN_KEY_ENV_VARS",
]
