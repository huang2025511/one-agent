"""Updater skill - Auto-update One-Agent from GitHub.

Usage: /update 或 /更新
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

from core.subprocess_utils import run_subprocess_async

logger = logging.getLogger(__name__)


def _find_project_root() -> Path:
    """从当前文件向上查找项目根目录（含有 .git 或 one_agent.py 的目录）。"""
    path = Path(__file__).resolve().parent
    for _ in range(5):
        if (path / ".git").exists() or (path / "one_agent.py").exists():
            return path
        path = path.parent
    # 回退：skills 目录的父目录的父目录（skills/updater.py -> skills/ -> project/）
    return Path(__file__).resolve().parent.parent


ROOT = _find_project_root()


def _run_git(args: list, timeout: int = 10) -> subprocess.CompletedProcess:
    """在项目根目录执行 git 命令。"""
    return subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=timeout,
    )


async def _run_git_async(args: list, timeout: int = 10) -> subprocess.CompletedProcess:
    """Async wrapper around _run_git — runs the blocking subprocess in a
    thread to avoid freezing the asyncio event loop.

    Use this from async contexts (e.g. skill handlers) instead of _run_git.
    """
    return await run_subprocess_async(
        ["git"] + args, timeout=timeout, cwd=str(ROOT)
    )


async def _run_subprocess_async(cmd: list, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a subprocess in a thread to avoid blocking the event loop."""
    return await run_subprocess_async(cmd, timeout=timeout)


def _detect_branch() -> str:
    """自动检测当前分支。失败则返回 'main'。"""
    try:
        result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "main"


def _detect_remote() -> str:
    """检测默认远程仓库名。"""
    try:
        result = _run_git(["remote"])
        if result.returncode == 0:
            remotes = result.stdout.strip().split("\n")
            if "origin" in remotes:
                return "origin"
            if remotes and remotes[0]:
                return remotes[0]
    except Exception:
        pass
    return "origin"


def make_updater_handler():
    """Create the updater skill handler."""

    async def handler(args: Dict[str, Any]) -> str:
        """Handle update request."""
        results = []
        results.append("🚀 开始更新 One-Agent...")
        results.append("")

        # 1. 检查 git 是否可用
        try:
            result = await _run_subprocess_async(["git", "--version"], timeout=5)
            if result.returncode != 0:
                raise FileNotFoundError("git not found")
            results.append(f"✓ Git 版本: {result.stdout.strip()}")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            results.append("⚠️ Git 未安装，尝试使用 curl 下载...")
            return await _update_with_curl(args.get("branch", "main"), results)

        results.append(f"📍 项目目录: {ROOT}")

        # 2. 检查是否为 git 仓库
        result = await _run_git_async(["rev-parse", "--is-inside-work-tree"])
        if result.returncode != 0 or result.stdout.strip() != "true":
            results.append("")
            results.append("❌ 当前目录不是 Git 仓库")
            results.append("")
            results.append("💡 请先在项目目录执行:")
            results.append(f"   cd {ROOT}")
            results.append("   git init")
            results.append("   git remote add origin <你的仓库地址>")
            results.append("   git fetch origin")
            results.append("   git checkout main")
            results.append("")
            results.append("或者使用 curl 方式更新...")
            return await _update_with_curl(args.get("branch", "main"), results)

        # 3. 检查远程仓库
        remote = _detect_remote()
        result = await _run_git_async(["remote", "-v"])
        if result.returncode != 0:
            results.append("❌ 无法获取远程仓库信息")
            results.append("")
            results.append("💡 请先在项目目录执行:")
            results.append(f"   cd {ROOT}")
            results.append("   git remote add origin <你的仓库地址>")
            results.append("   git fetch origin")
            return "\n".join(results)

        remote_url = ""
        for line in result.stdout.strip().split("\n"):
            if remote in line and "fetch" in line:
                parts = line.split()
                if len(parts) > 1:
                    remote_url = parts[1]
                break

        if not remote_url:
            results.append(f"❌ 未找到 {remote} 远程仓库")
            results.append("")
            results.append("💡 请添加远程仓库:")
            results.append(f"   cd {ROOT}")
            results.append("   git remote add origin <你的仓库地址>")
            return "\n".join(results)

        results.append(f"🌐 远程仓库: {remote_url}")

        # 4. 自动检测分支
        branch = args.get("branch") or _detect_branch()
        results.append(f"🔖 当前分支: {branch}")

        # 5. 获取远程最新版本
        results.append("")
        results.append("📡 正在获取远程最新版本...")
        try:
            result = await _run_git_async(["fetch", remote, branch], timeout=60)
            if result.returncode != 0:
                err = result.stderr.strip() or "fetch failed"
                results.append(f"⚠️ Fetch 失败: {err}")
                results.append("")
                results.append("💡 可能的原因:")
                results.append("   1. 网络不通 - 检查网络连接")
                results.append("   2. 分支不存在 - 确认远程分支名")
                results.append("   3. 需要认证 - 私有仓库请配置 SSH 或 token")
                results.append("")
                results.append("尝试使用 curl 方式更新...")
                return await _update_with_curl(branch, results)

            result = await _run_git_async(["log", "-1", "--format=%H %s", f"{remote}/{branch}"])
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split(" ", 1)
                if len(parts) == 2:
                    commit_hash, commit_msg = parts
                    results.append(f"📋 远程最新: {commit_hash[:8]}")
                    results.append(f"   {commit_msg}")
            else:
                results.append("⚠️ 无法获取远程版本信息")
        except subprocess.TimeoutExpired:
            results.append("⚠️ 网络超时，尝试 curl 方式...")
            return await _update_with_curl(branch, results)
        except Exception as e:
            results.append(f"⚠️ 获取版本失败: {e}")

        # 6. 获取当前版本
        results.append("")
        results.append("📌 当前版本:")
        result = await _run_git_async(["log", "-1", "--format=%H %s"])
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split(" ", 1)
            if len(parts) == 2:
                ch, cm = parts
                results.append(f"   {ch[:8]} - {cm}")

        # 7. 拉取更新
        results.append("")
        results.append("📥 正在拉取更新...")
        try:
            result = await _run_git_async(["status", "--porcelain"])
            has_changes = result.stdout.strip()

            if has_changes:
                results.append("⚠️ 检测到本地修改，自动 stash...")
                await _run_git_async(["stash"])

            result = await _run_git_async(["pull", remote, branch], timeout=120)

            if result.returncode == 0:
                results.append("✓ 代码更新成功!")
                result = await _run_git_async(["log", "-1", "--format=%H"])
                if result.returncode == 0 and result.stdout.strip():
                    results.append(f"   新版本: {result.stdout.strip()[:8]}")

                # 检查是否有依赖变化
                result = await _run_git_async(
                    ["diff", "--name-only", "ORIG_HEAD", "HEAD", "--",
                     "requirements.txt", "pyproject.toml"]
                )
                deps_changed = result.stdout.strip()
                if deps_changed:
                    results.append("")
                    results.append("📦 检测到依赖文件变化，正在更新...")
                    pip_cmd = sys.executable
                    try:
                        r = await _run_subprocess_async(
                            [pip_cmd, "-m", "pip", "install", "-r",
                             str(ROOT / "requirements.txt")],
                            timeout=180,
                        )
                        if r.returncode == 0:
                            results.append("✓ 依赖更新成功!")
                        else:
                            results.append(f"⚠️ 依赖更新部分失败: {r.stderr.strip()[:200]}")
                    except Exception as e:
                        results.append(f"⚠️ 依赖更新失败: {e}")
            else:
                err = result.stderr.strip() or "pull failed"
                results.append(f"❌ 更新失败: {err}")
                results.append("")
                results.append("💡 如果本地有冲突，可手动执行:")
                results.append(f"   cd {ROOT}")
                results.append(f"   git reset --hard {remote}/{branch}")
                return "\n".join(results)

        except subprocess.TimeoutExpired:
            results.append("⚠️ pull 超时，可能是网络问题")
            return "\n".join(results)
        except Exception as e:
            results.append(f"❌ 拉取更新失败: {e}")
            return "\n".join(results)

        # 8. 完成
        results.append("")
        results.append("=" * 50)
        results.append("✅ 更新完成!")
        results.append("")
        results.append("💡 如需重启以应用更新，请输入: /restart 或 /重启")
        results.append("=" * 50)

        return "\n".join(results)

    return handler


async def _update_with_curl(branch: str, results: list) -> str:
    """Fallback update using curl when git is not available."""
    results.append("")
    results.append("📥 使用 curl 方式更新（仅更新核心文件）...")
    results.append(f"📍 项目目录: {ROOT}")

    try:
        # 从当前 git remote 或使用默认地址
        remote_url = ""
        try:
            result = await _run_git_async(["remote", "-v"])
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if "origin" in line and "fetch" in line:
                        parts = line.split()
                        if len(parts) > 1:
                            remote_url = parts[1]
                        break
        except Exception:
            pass

        # 从 remote URL 提取 user/repo
        gh_prefix = "https://github.com/"
        gh_suffix = ".git"
        repo_path = ""

        if remote_url:
            if remote_url.startswith(gh_prefix):
                repo_path = remote_url[len(gh_prefix):]
                if repo_path.endswith(gh_suffix):
                    repo_path = repo_path[:-len(gh_suffix)]
            elif remote_url.startswith("git@github.com:"):
                repo_path = remote_url[len("git@github.com:"):]
                if repo_path.endswith(gh_suffix):
                    repo_path = repo_path[:-len(gh_suffix)]

        if not repo_path:
            results.append("⚠️ 无法从远程 URL 解析 GitHub 地址")
            results.append("💡 请确保项目目录中已正确配置 git remote")
            results.append("   或手动执行: git pull <remote> <branch>")
            return "\n".join(results)

        raw_url = f"https://raw.githubusercontent.com/{repo_path}/{branch}"
        results.append(f"🌐 下载来源: {raw_url}")
        results.append("")

        files_to_update = [
            ("one_agent.py", "主程序"),
            ("install", "安装脚本"),
            ("requirements.txt", "依赖列表"),
            ("skills/updater.py", "更新技能"),
            ("skills/wechat_login.py", "微信技能"),
            ("skills/__init__.py", "技能注册"),
            ("scripts/fetch_models.py", "模型获取脚本"),
            ("core/coordinator.py", "协调器"),
        ]

        ok_count = 0
        fail_count = 0
        for file_path, desc in files_to_update:
            results.append(f"   下载 {desc}...")
            url = f"{raw_url}/{file_path}"
            local_path = ROOT / file_path
            local_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                r = await _run_subprocess_async(
                    ["curl", "-s", "-L", "-f", "-o", str(local_path), url],
                    timeout=30,
                )
                if r.returncode == 0 and local_path.exists() and local_path.stat().st_size > 0:
                    results.append(f"   ✓ {desc} 更新成功")
                    ok_count += 1
                else:
                    results.append(f"   ⚠️ {desc} 下载失败")
                    fail_count += 1
            except Exception as e:
                results.append(f"   ⚠️ {desc} 下载失败: {e}")
                fail_count += 1

        results.append("")
        if ok_count > 0:
            results.append(f"✅ curl 方式更新完成 (成功 {ok_count} 个, 失败 {fail_count} 个)")
            results.append("💡 完整更新请使用 Git 方式")
            results.append("💡 如需重启以应用更新，请输入: /restart 或 /重启")
        else:
            results.append("❌ curl 方式更新全部失败")
            results.append("💡 请检查网络或手动下载:")
            results.append(f"   {raw_url}")

    except Exception as e:
        results.append(f"❌ curl 更新失败: {e}")

    return "\n".join(results)


# Skill definition for registration
UPDATER_SKILL = {
    "id": "updater",
    "title": "更新 One-Agent",
    "description": "/update 或 /更新：从 GitHub 更新到最新版本，支持 Git 和 curl 两种方式",
    "schema": {
        "type": "object",
        "properties": {
            "branch": {
                "type": "string",
                "description": "分支名称（可选，默认自动检测）",
                "default": ""
            }
        }
    },
    "handler_maker": make_updater_handler,
}
