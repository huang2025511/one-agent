"""Updater skill - Auto-update One-Agent from GitHub.

Usage: 输入 "更新" 或 "升级" 即可自动更新到最新版本
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

# 获取项目根目录
ROOT = Path(__file__).parent.parent.parent


def make_updater_handler():
    """Create the updater skill handler."""
    
    async def handler(args: Dict[str, Any]) -> str:
        """Handle update request."""
        branch = args.get("branch", "main")
        auto_restart = args.get("auto_restart", True)
        
        results = []
        results.append("🚀 开始更新 One-Agent...")
        results.append("")
        
        # 1. 检查 git 是否可用
        try:
            result = subprocess.run(
                ["git", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                raise FileNotFoundError("git not found")
            results.append(f"✓ Git 版本: {result.stdout.strip()}")
        except FileNotFoundError:
            results.append("⚠️ Git 未安装，尝试使用 curl 下载...")
            return await _update_with_curl(branch, results)
        
        # 2. 检查远程仓库
        try:
            result = subprocess.run(
                ["git", "remote", "-v"],
                capture_output=True,
                text=True,
                cwd=ROOT,
                timeout=5
            )
            if result.returncode != 0:
                results.append("❌ 无法获取远程仓库信息")
                return "\n".join(results)
            
            # 解析远程仓库 URL
            remote_url = ""
            for line in result.stdout.strip().split("\n"):
                if "origin" in line and "fetch" in line:
                    remote_url = line.split()[1] if len(line.split()) > 1 else ""
                    break
            
            if not remote_url:
                results.append("❌ 未找到 origin 远程仓库")
                return "\n".join(results)
            
            results.append(f"✓ 远程仓库: {remote_url}")
        except Exception as e:
            results.append(f"❌ 检查远程仓库失败: {e}")
            return "\n".join(results)
        
        # 3. 获取远程最新版本
        results.append("")
        results.append("📡 正在获取远程最新版本...")
        try:
            # Fetch latest
            result = subprocess.run(
                ["git", "fetch", "origin", branch],
                capture_output=True,
                text=True,
                cwd=ROOT,
                timeout=30
            )
            if result.returncode != 0:
                results.append(f"⚠️ Fetch 失败: {result.stderr.strip()}")
            
            # 获取最新 commit
            result = subprocess.run(
                ["git", "log", "-1", "--format=%H %s", f"origin/{branch}"],
                capture_output=True,
                text=True,
                cwd=ROOT,
                timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                latest_commit = result.stdout.strip().split(" ", 1)
                if len(latest_commit) == 2:
                    commit_hash, commit_msg = latest_commit
                    results.append(f"📋 最新版本: {commit_hash[:8]}")
                    results.append(f"   {commit_msg}")
            else:
                results.append("⚠️ 无法获取最新版本信息")
        except Exception as e:
            results.append(f"⚠️ 获取版本失败: {e}")
        
        # 4. 获取当前版本
        results.append("")
        results.append("📌 当前版本:")
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%H %s"],
                capture_output=True,
                text=True,
                cwd=ROOT,
                timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                current = result.stdout.strip().split(" ", 1)
                if len(current) == 2:
                    current_hash, current_msg = current
                    results.append(f"   {current_hash[:8]} - {current_msg}")
        except Exception:
            pass
        
        # 5. 拉取更新
        results.append("")
        results.append("📥 正在拉取更新...")
        try:
            # 先 stash 本地更改（如果有）
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                cwd=ROOT,
                timeout=10
            )
            has_changes = result.stdout.strip()
            
            if has_changes:
                results.append("⚠️ 检测到本地修改，自动 stash...")
                subprocess.run(
                    ["git", "stash"],
                    capture_output=True,
                    cwd=ROOT,
                    timeout=10
                )
            
            # Pull
            result = subprocess.run(
                ["git", "pull", "origin", branch],
                capture_output=True,
                text=True,
                cwd=ROOT,
                timeout=60
            )
            
            if result.returncode == 0:
                results.append("✓ 代码更新成功!")
                
                # 检查更新的文件
                result = subprocess.run(
                    ["git", "log", "-1", "--format=%H", "HEAD"],
                    capture_output=True,
                    text=True,
                    cwd=ROOT,
                    timeout=10
                )
                if result.returncode == 0:
                    new_hash = result.stdout.strip()[:8]
                    results.append(f"   新版本: {new_hash}")
            else:
                results.append(f"❌ 更新失败: {result.stderr.strip()}")
                return "\n".join(results)
                
        except Exception as e:
            results.append(f"❌ 拉取更新失败: {e}")
            return "\n".join(results)
        
        # 6. 检查是否需要更新依赖
        results.append("")
        results.append("📦 检查依赖更新...")
        try:
            # 检查 requirements.txt 是否有变化
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~1", "requirements.txt", "pyproject.toml"],
                capture_output=True,
                text=True,
                cwd=ROOT,
                timeout=10
            )
            deps_changed = result.stdout.strip()
            
            if deps_changed:
                results.append("   检测到依赖文件有变化")
                results.append("   运行 pip install -r requirements.txt ...")
                
                # 确定 pip 命令
                pip_cmd = sys.executable
                result = subprocess.run(
                    [pip_cmd, "-m", "pip", "install", "-r", "requirements.txt", "--quiet"],
                    capture_output=True,
                    timeout=120
                )
                if result.returncode == 0:
                    results.append("✓ 依赖更新成功!")
                else:
                    results.append(f"⚠️ 依赖更新失败: {result.stderr.decode()[:200] if result.stderr else 'unknown'}")
            else:
                results.append("   依赖无变化，跳过")
        except Exception as e:
            results.append(f"⚠️ 依赖检查失败: {e}")
        
        # 7. 完成
        results.append("")
        results.append("=" * 50)
        results.append("✅ 更新完成!")
        results.append("")
        results.append("💡 如需重启以应用更新，请输入: 重启")
        results.append("=" * 50)
        
        return "\n".join(results)
    
    return handler


async def _update_with_curl(branch: str, results: list) -> str:
    """Fallback update using curl when git is not available."""
    results.append("")
    results.append("📥 使用 curl 方式更新...")
    
    try:
        # 获取 repo 信息
        raw_url = f"https://raw.githubusercontent.com/huang2025511/one-agent/{branch}"
        
        # 下载主要文件
        files_to_update = [
            ("one_agent.py", "主程序"),
            ("install", "安装脚本"),
            ("scripts/fetch_models.py", "模型获取脚本"),
        ]
        
        for file_path, desc in files_to_update:
            results.append(f"   下载 {desc}...")
            url = f"{raw_url}/{file_path}"
            local_path = ROOT / file_path
            
            result = subprocess.run(
                ["curl", "-s", "-L", "-o", str(local_path), url],
                capture_output=True,
                timeout=30
            )
            
            if result.returncode == 0 and local_path.exists():
                results.append(f"   ✓ {desc} 更新成功")
            else:
                results.append(f"   ⚠️ {desc} 下载失败")
        
        results.append("")
        results.append("✅ 使用 curl 方式更新完成!")
        results.append("💡 如需完整更新，请安装 git: apt install git")
        
    except Exception as e:
        results.append(f"❌ curl 更新失败: {e}")
    
    return "\n".join(results)


# Skill definition for registration
UPDATER_SKILL = {
    "id": "updater",
    "title": "更新 One-Agent",
    "description": "更新 One-Agent 到最新版本，支持 Git 和 curl 两种方式",
    "schema": {
        "type": "object",
        "properties": {
            "branch": {
                "type": "string",
                "description": "分支名称，默认 main",
                "default": "main"
            },
            "auto_restart": {
                "type": "boolean",
                "description": "更新后是否自动重启",
                "default": True
            }
        }
    },
    "handler_maker": make_updater_handler,
}
