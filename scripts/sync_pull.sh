#!/usr/bin/env bash
# ============================================================
# 同步工作流 - 从 Gitee 拉取最新代码
# 原则：Gitee 为主仓库，GitHub 仅作备份和 APK 构建
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

echo "=========================================="
echo " 从 Gitee 拉取最新代码 (Gitee = 主仓库)"
echo "=========================================="

# 1. 检查 gitee remote 是否存在
if ! git remote get-url gitee &>/dev/null; then
    echo "[!] 未找到 gitee 远程仓库，正在添加..."
    git remote add gitee https://gitee.com/huang20260511/one-agent.git
fi

# 2. 检查当前是否在 main 分支
CURRENT_BRANCH=$(git branch --show-current)
if [ "$CURRENT_BRANCH" != "main" ]; then
    echo "[!] 当前分支: $CURRENT_BRANCH，切换到 main..."
    git checkout main
fi

# 3. 检查工作区是否干净
if ! git diff-index --quiet HEAD --; then
    echo "[!] 检测到本地未提交的修改，正在 stash..."
    git stash push -m "auto-stash before gitee pull $(date '+%Y%m%d-%H%M%S')"
    STASHED=1
else
    STASHED=0
fi

# 4. 拉取 Gitee 最新代码
echo "[*] 正在拉取 Gitee main..."
git fetch gitee main

# 5. 合并 Gitee 代码（Gitee 为主，冲突时以 Gitee 为准）
LOCAL_BEFORE=$(git rev-parse HEAD)
GITEE_HEAD=$(git rev-parse gitee/main)

if [ "$LOCAL_BEFORE" = "$GITEE_HEAD" ]; then
    echo "[✓] 本地已是最新，无需更新"
else
    echo "[*] 合并 Gitee 最新提交..."
    if git merge-base --is-ancestor "$LOCAL_BEFORE" "$GITEE_HEAD"; then
        # 快进合并
        git merge --ff-only gitee/main
    else
        # 有分叉，合并时冲突以 Gitee 为准 (theirs strategy)
        git merge gitee/main --no-edit -X theirs || {
            echo "[!] 仍有冲突，使用 Gitee 版本解决..."
            git checkout --theirs .
            git add .
            git commit --no-edit
        }
    fi
    echo "[✓] 已同步到 Gitee 最新版本"
fi

# 6. 恢复 stash（如有）
if [ "$STASHED" = "1" ]; then
    echo "[*] 恢复本地修改..."
    if git stash pop; then
        echo "[✓] 本地修改已恢复"
    else
        echo "[!] stash 应用时有冲突，请手动解决"
        echo "    查看: git stash list"
    fi
fi

echo ""
echo "=========================================="
echo " 同步完成"
echo "=========================================="
echo "当前版本: $(git rev-parse --short HEAD)"
git log --oneline -3
