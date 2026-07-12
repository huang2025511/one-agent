#!/usr/bin/env bash
# ============================================================
# 同步工作流 - 推送到 Gitee + GitHub
# 原则：Gitee 为主仓库，GitHub 仅作备份和 APK 构建
# 先推 Gitee，成功后再推 GitHub
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

echo "=========================================="
echo " 推送到 Gitee + GitHub (Gitee 为主)"
echo "=========================================="

# 1. 检查是否在 main 分支
CURRENT_BRANCH=$(git branch --show-current)
if [ "$CURRENT_BRANCH" != "main" ]; then
    echo "[!] 当前分支: $CURRENT_BRANCH，请先切换到 main"
    exit 1
fi

# 2. 检查工作区是否干净
if ! git diff-index --quiet HEAD --; then
    echo "[!] 工作区有未提交的修改，请先提交或 stash"
    git status --short
    exit 1
fi

# 3. 检查 gitee remote
if ! git remote get-url gitee &>/dev/null; then
    echo "[!] 未找到 gitee 远程仓库，正在添加..."
    git remote add gitee https://gitee.com/huang20260511/one-agent.git
fi

# 4. 检查 origin (GitHub) remote
if ! git remote get-url origin &>/dev/null; then
    echo "[!] 未找到 origin (GitHub) 远程仓库，正在添加..."
    git remote add origin https://github.com/huang2025511/one-agent.git
fi

# 5. 推送到 Gitee（主仓库，必须成功）
echo ""
echo "[1/2] 推送到 Gitee (主仓库)..."
if git push gitee main; then
    echo "[✓] Gitee 推送成功"
else
    echo "[✗] Gitee 推送失败！"
    echo "    可能需要先运行 scripts/sync_pull.sh 同步 Gitee 最新代码"
    exit 1
fi

# 6. 推送到 GitHub（备份仓库，失败不阻断但会告警）
echo ""
echo "[2/2] 推送到 GitHub (备份/APK构建)..."
if git push origin main; then
    echo "[✓] GitHub 推送成功"
else
    echo "[!] GitHub 推送失败（非致命，Gitee 已更新）"
    echo "    请检查 GitHub 网络连接或权限"
fi

echo ""
echo "=========================================="
echo " 推送完成"
echo "=========================================="
echo "当前版本: $(git rev-parse --short HEAD)"
echo "Gitee : https://gitee.com/huang20260511/one-agent"
echo "GitHub: https://github.com/huang2025511/one-agent"
