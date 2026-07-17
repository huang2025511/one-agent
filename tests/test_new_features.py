"""Tests for setup wizard, WeChat personal gateway, and system executor.

Covers:
  - API key detection logic
  - Setup wizard CLI / web endpoints
  - Personal WeChat gateway lifecycle
  - System executor risk classification
  - Password caching / lockout / verification
  - Dangerous command detection
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ============================================================
# 1. Setup Wizard — API key detection
# ============================================================
def test_has_api_key_with_env_var():
    """_has_any_api_key returns True when env var is set."""
    from core.setup_wizard import _has_any_api_key
    os.environ["OPENAI_API_KEY"] = "sk-test12345678901234567890"
    try:
        assert _has_any_api_key() is True
    finally:
        del os.environ["OPENAI_API_KEY"]


def test_has_api_key_false_when_none():
    """_has_any_api_key returns False when no env var is set."""
    from core.setup_wizard import _has_any_api_key
    # Clear any keys that might be set
    for var in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SENSENOVA_API_KEY",
                "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ONE_AGENT_API_KEY"]:
        os.environ.pop(var, None)
    assert _has_any_api_key() is False


def test_setup_wizard_smoke():
    """Import smoke test — ensures no syntax errors."""
    from core.setup_wizard import (
        _has_any_api_key,
        register_setup_endpoints,
        run_cli_setup,
        setup_if_needed,
    )
    assert callable(setup_if_needed)
    assert callable(run_cli_setup)
    assert callable(register_setup_endpoints)
    assert callable(_has_any_api_key)


def test_setup_wizard_non_interactive():
    """When stdin is not a TTY, setup_if_needed returns False."""
    from core.setup_wizard import setup_if_needed
    # We can't simulate non-TTY easily, but check that it doesn't crash
    # when called without env vars
    old = os.environ.pop("OPENAI_API_KEY", None)
    try:
        result = setup_if_needed()
        assert isinstance(result, bool)
    finally:
        if old:
            os.environ["OPENAI_API_KEY"] = old


def test_setup_html_endpoint():
    """The /setup endpoint returns valid HTML."""
    from core.setup_wizard import _SETUP_HTML
    assert "<html" in _SETUP_HTML.lower()
    assert "setup" in _SETUP_HTML.lower()
    assert "API Key" in _SETUP_HTML


# ============================================================
# 2. Personal WeChat Gateway
# ============================================================
def test_wechat_personal_disabled_by_default():
    """Without config, the gateway should not start."""
    from gateways.wechat_personal import WeChatPersonalGateway
    gw = WeChatPersonalGateway()
    assert gw.name == "gateway_wechat_personal"
    assert gw._enabled is False


def test_wechat_personal_lifecycle_methods():
    """Gateway has setup/stop/send."""
    from gateways.wechat_personal import WeChatPersonalGateway
    gw = WeChatPersonalGateway()
    assert hasattr(gw, "setup")
    assert hasattr(gw, "stop")
    # 修复测试 bug：之前断言 hasattr(gw, "_send_text")，但全仓库没有任何
    # gateway 定义这个方法（也无此约定）。WeChatPersonalGateway 实际的
    # 发消息入口是 async def send(self, chat_id, text)（见
    # gateways/wechat_personal.py）。其它 gateway 的私有发消息方法命名
    # 各异（_send / _send_message / _send_app_message），没有统一约定。
    assert hasattr(gw, "send")


# ============================================================
# 3. System Executor — risk classification
# ============================================================
def test_classify_safe_commands():
    """Safe commands (ls, cat, echo, date, whoami) are level 0."""
    from executors.system import classify_command
    safe_cmds = [
        "ls",
        "ls -la /tmp",
        "cat /etc/hostname",
        "head -n 10 /var/log/syslog",
        "echo hello world",
        "date",
        "uptime",
        "free -h",
        "df -h",
        "whoami",
        "id",
        "uname -a",
        "pwd",
        "wc -l /etc/passwd",
    ]
    for cmd in safe_cmds:
        level, reason = classify_command(cmd)
        assert level == 0, f"{cmd!r} should be SAFE, got {level}: {reason}"


def test_classify_low_commands():
    """File write/git operations are level 1."""
    from executors.system import classify_command
    low_cmds = [
        "mkdir /tmp/test",
        "touch /tmp/test.txt",
        "cp file1 file2",
        "mv old new",
        "git clone https://github.com/example/repo.git",
        "git pull origin main",
        "git log --oneline -10",
        "git diff HEAD~1",
        "tar -czf archive.tar.gz dir/",
        "unzip archive.zip",
        "tee output.txt",
    ]
    for cmd in low_cmds:
        level, reason = classify_command(cmd)
        assert level == 1, f"{cmd!r} should be LOW, got {level}: {reason}"


def test_classify_medium_commands():
    """Package install / service manage are level 2."""
    from executors.system import classify_command
    medium_cmds = [
        "pip install requests",
        "npm install express",
        "systemctl start nginx",
        "service ssh status",
        "docker ps",
        "docker pull alpine",
        "chown user:group /path",
        "kill 1234",
        "pkill python",
    ]
    for cmd in medium_cmds:
        level, reason = classify_command(cmd)
        assert level == 2, f"{cmd!r} should be MEDIUM, got {level}: {reason}"


def test_classify_dangerous_commands():
    """Destructive commands are level 3."""
    from executors.system import classify_command
    dangerous_cmds = [
        "rm -rf /",
        "rm -r /tmp/important",
        "sudo ls",
        "sudo apt-get install python",
        "chmod 777 /etc/passwd",
        "shutdown -h now",
        "reboot",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sda",
        "fdisk /dev/sda",
        "> /dev/null && cat /etc/shadow",
        "iptables -F",
        "crontab -e",
    ]
    for cmd in dangerous_cmds:
        level, reason = classify_command(cmd)
        assert level == 3, f"{cmd!r} should be DANGEROUS, got {level}: {reason}"


def test_classify_unknown_is_medium():
    """Unknown commands default to MEDIUM (level 2)."""
    from executors.system import classify_command
    level, reason = classify_command("some_random_command_xyz --flag arg")
    assert level == 2, f"unknown command should be MEDIUM, got {level}"
    assert "unknown" in reason.lower()


def test_classify_empty_command():
    """Empty command is SAFE."""
    from executors.system import classify_command
    level, reason = classify_command("")
    assert level == 0
    level, reason = classify_command("   ")
    assert level == 0


# ============================================================
# 4. Password Manager
# ============================================================
def test_password_hash_and_verify():
    """Password hashing and verification work correctly."""
    from executors.system import PasswordManager
    plain = "my_secure_password"
    hash_val = hashlib.sha256(plain.encode()).hexdigest()
    mgr = PasswordManager(hash_val)
    assert mgr.is_configured() is True
    assert mgr.verify(plain) is True
    assert mgr.verify("wrong_password") is False


def test_password_no_hash_rejects_all():
    """When no password hash is set, verify() rejects everything.

    修复说明：之前未配置密码时 verify() 返回 True，构成认证绕过——
    任何 DANGEROUS 命令（rm -rf, mkfs 等）都能被任意非空密码解锁。
    修复后：未配置密码 → 拒绝所有需要密码的操作，强制用户先配置密码。
    """
    from executors.system import PasswordManager
    mgr = PasswordManager("")
    assert mgr.is_configured() is False
    # 未配置密码 → 拒绝（修复后行为）
    assert mgr.verify("anything") is False
    assert mgr.verify("anything_else") is False
    assert mgr.verify("") is False


def test_password_cache():
    """Password cache works for the configured duration."""
    from executors.system import PasswordManager
    plain = "test123"
    hash_val = hashlib.sha256(plain.encode()).hexdigest()
    mgr = PasswordManager(hash_val, cache_minutes=1)
    mgr.verify(plain)
    mgr.record_success(1)
    assert mgr.is_cached(1) is True
    assert mgr.is_cached(2) is False  # cached at level 1, not 2


def test_password_lockout():
    """Three failed attempts trigger lockout."""
    from executors.system import PasswordManager
    hash_val = hashlib.sha256(b"secret").hexdigest()
    mgr = PasswordManager(hash_val, max_attempts=3, lockout_minutes=1)
    assert mgr.can_attempt() is True
    mgr.verify("wrong1")
    mgr.record_failure()
    assert mgr.can_attempt() is True
    mgr.verify("wrong2")
    mgr.record_failure()
    assert mgr.can_attempt() is True
    mgr.verify("wrong3")
    mgr.record_failure()
    assert mgr.can_attempt() is False  # locked out


def test_password_invalidate_cache():
    """Cache can be invalidated."""
    from executors.system import PasswordManager
    hash_val = hashlib.sha256(b"secret").hexdigest()
    mgr = PasswordManager(hash_val)
    mgr.record_success(3)
    assert mgr.is_cached(3) is True
    mgr.invalidate_cache()
    assert mgr.is_cached(0) is False


def test_password_success_resets_failures():
    """A successful verification resets the failure counter."""
    from executors.system import PasswordManager
    hash_val = hashlib.sha256(b"secret").hexdigest()
    mgr = PasswordManager(hash_val)
    mgr.record_failure()
    mgr.record_failure()
    assert mgr._failed_count == 2
    mgr.verify("secret")
    mgr.record_success(1)
    assert mgr._failed_count == 0


# ============================================================
# 5. SystemExecutor plugin lifecycle
# ============================================================
def test_system_executor_disabled_by_default():
    """Without password config, executor starts but allows SAFE only."""
    from executors.system import SystemExecutor
    exe = SystemExecutor()
    assert exe.name == "system_executor"
    assert exe._enabled is False


def test_system_executor_dispatch_missing_command():
    """Dispatch rejects empty/missing command."""
    from executors.system import SystemExecutor
    exe = SystemExecutor()
    result = asyncio.run(exe.dispatch("system.run", {}))
    assert result["ok"] is False
    assert "command" in str(result.get("error", "")).lower()


def test_system_executor_safe_command_allowed():
    """SAFE commands should be allowed without password."""
    from executors.system import SystemExecutor
    exe = SystemExecutor()
    exe._enabled = True
    allowed, needs_pwd = asyncio.run(
        exe._check_permission("echo hello", 0, "safe", "")
    )
    assert allowed is True
    assert needs_pwd is False


def test_system_executor_dangerous_allowed_without_password():
    """密码保护恢复：DANGEROUS 命令默认需密码，未配置密码时拒绝。

    修复 #2（安全）：之前 _check_permission 无条件放行所有命令（含 DANGEROUS），
    使 PasswordManager 成为死代码。现在恢复分级判断——risk>=2 且 require_password=True
    时，未配置密码则拒绝（fail-secure），需要密码输入。
    """
    from executors.system import SystemExecutor
    exe = SystemExecutor()
    exe._enabled = True
    # 默认 require_password=True，且 _pwd_manager=None（未 setup）
    # → 高危命令应被拒绝并要求密码
    allowed, needs_pwd = asyncio.run(
        exe._check_permission("rm -rf /tmp/test", 3, "dangerous", "")
    )
    assert allowed is False, "DANGEROUS 命令在未配置密码时应被拒绝（fail-secure）"
    assert needs_pwd is True, "应提示需要密码输入"

    # 关闭密码保护开关后，DANGEROUS 命令直接放行
    exe._require_password = False
    allowed2, needs_pwd2 = asyncio.run(
        exe._check_permission("rm -rf /tmp/test", 3, "dangerous", "")
    )
    assert allowed2 is True
    assert needs_pwd2 is False


def test_system_executor_static_hash():
    """Static hash_password utility returns a verifiable PBKDF2 hash."""
    from executors.system import SystemExecutor
    h1 = SystemExecutor.hash_password("hello")
    h2 = SystemExecutor.hash_password("hello")
    # PBKDF2 uses a random salt, so two hashes differ but both verify.
    assert h1 != h2
    assert h1.startswith("pbkdf2_sha256$")
    # Both must verify against the same plaintext.
    from executors.system import PasswordManager
    assert PasswordManager(h1).verify("hello") is True
    assert PasswordManager(h2).verify("hello") is True
    assert PasswordManager(h1).verify("wrong") is False


# ============================================================
# 6. CLI entry point smoke
# ============================================================
def test_main_module_exists():
    """__main__.py exists and is importable."""
    import __main__
    assert hasattr(__main__, "__file__")


# ============================================================
# 7. Config security section
# ============================================================
def test_config_security_section():
    """default_config.yaml has system_executor_password field."""
    import yaml
    cfg = yaml.safe_load(open(ROOT / "config" / "default_config.yaml"))
    sec = cfg.get("security", {})
    assert "system_executor_password" in sec
    assert "password_cache_minutes" in sec
    assert "require_password_for_dangerous" in sec


def test_config_wechat_personal_section():
    """default_config.yaml has wechat_personal gateway section."""
    import yaml
    cfg = yaml.safe_load(open(ROOT / "config" / "default_config.yaml"))
    gateways = cfg.get("gateways", {})
    wxp = gateways.get("wechat_personal", {})
    assert "enabled" in wxp
    assert wxp["enabled"] is False


# ============================================================
# Runner
# ============================================================
def main() -> int:
    print("\n=== new features test ===")
    tests = [
        ("api key env var", test_has_api_key_with_env_var),
        ("api key none", test_has_api_key_false_when_none),
        ("setup wizard import", test_setup_wizard_smoke),
        ("setup non-interactive", test_setup_wizard_non_interactive),
        ("setup HTML", test_setup_html_endpoint),
        ("wechat personal import", test_wechat_personal_disabled_by_default),
        ("wechat personal lifecycle", test_wechat_personal_lifecycle_methods),
        ("classify safe", test_classify_safe_commands),
        ("classify low", test_classify_low_commands),
        ("classify medium", test_classify_medium_commands),
        ("classify dangerous", test_classify_dangerous_commands),
        ("classify unknown", test_classify_unknown_is_medium),
        ("classify empty", test_classify_empty_command),
        ("password hash+verify", test_password_hash_and_verify),
        ("password no hash", test_password_no_hash_rejects_all),
        ("password cache", test_password_cache),
        ("password lockout", test_password_lockout),
        ("password invalidate", test_password_invalidate_cache),
        ("password success resets", test_password_success_resets_failures),
        ("sys exec dispatch", test_system_executor_dispatch_missing_command),
        ("sys exec safe allowed", test_system_executor_safe_command_allowed),
        ("sys exec dangerous allowed", test_system_executor_dangerous_allowed_without_password),
        ("sys exec static hash", test_system_executor_static_hash),
        ("config security", test_config_security_section),
        ("config wechat personal", test_config_wechat_personal_section),
    ]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except Exception as exc:
            print(f"  ✗ {name}: {exc}")
            import traceback
            traceback.print_exc()
    print(f"\n{passed}/{len(tests)} tests passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())

# Also make it runnable with pytest
def test_all_features():
    """Entry point for pytest discovery."""
    assert main() == 0
