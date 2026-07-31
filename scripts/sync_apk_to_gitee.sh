#!/usr/bin/env bash
# ============================================================
# 同步 APK: GitHub Release → Gitee Release
# ============================================================
# 背景：国内下载 GitHub Release 太慢，需要把 APK 镜像到 Gitee Release。
# 原因：GitHub Actions runner 在海外，跨境上传 58MB APK 到 Gitee 经常超时。
# 方案：用户本地运行此脚本（国内网络），从 GitHub Release 下载 APK，
#       再上传到 Gitee Release。整条链路走国内网络，稳定快速。
#
# 使用方法：
#   1. 设置环境变量（也可临时输入）：
#      export GITEE_TOKEN=<你的 Gitee Token>
#      export GH_TOKEN=<你的 GitHub Token，仅下载私有仓库需要，公开仓库可省略>
#   2. 运行：./scripts/sync_apk_to_gitee.sh [tag版本号]
#      不传参数则同步最新含 pet 的 Release
#
# 示例：
#   ./scripts/sync_apk_to_gitee.sh           # 同步最新 pet Release
#   ./scripts/sync_apk_to_gitee.sh v0.1.13-pet  # 同步指定 tag
# ============================================================
set -e

GH_REPO="huang2025511/one-agent"
GITEE_REPO="huang20260511/one-agent"
GITEE_USER="huang20260511"

# 检查依赖
command -v curl >/dev/null || { echo "[✗] 需要 curl"; exit 1; }
command -v jq   >/dev/null || { echo "[✗] 需要 jq (apt install jq)"; exit 1; }

# 检查 Gitee Token
if [ -z "$GITEE_TOKEN" ]; then
    echo "[?] 未设置 GITEE_TOKEN 环境变量"
    read -s -p "请输入 Gitee Token: " GITEE_TOKEN
    echo ""
    if [ -z "$GITEE_TOKEN" ]; then
        echo "[✗] Gitee Token 不能为空"
        exit 1
    fi
fi

# 1. 获取目标 tag
TARGET_TAG="${1:-}"
if [ -z "$TARGET_TAG" ]; then
    echo "[1/4] 获取 GitHub 最新 pet Release..."
    API_URL="https://api.github.com/repos/${GH_REPO}/releases?per_page=10"
    if [ -n "$GH_TOKEN" ]; then
        RELEASE_INFO=$(curl -s -H "Authorization: token $GH_TOKEN" "$API_URL")
    else
        RELEASE_INFO=$(curl -s "$API_URL")
    fi
    TARGET_TAG=$(echo "$RELEASE_INFO" | jq -r '.[] | select(.tag_name | contains("pet")) | .tag_name' | head -1)
    if [ -z "$TARGET_TAG" ] || [ "$TARGET_TAG" = "null" ]; then
        echo "[✗] 未找到 pet Release"
        echo "请指定 tag：$0 v0.1.13-pet"
        exit 1
    fi
    echo "    目标 tag: $TARGET_TAG"
fi

# 2. 获取 Release 信息和 APK
echo "[2/4] 获取 Release ${TARGET_TAG} 的 APK 信息..."
if [ -n "$GH_TOKEN" ]; then
    RELEASE_INFO=$(curl -s -H "Authorization: token $GH_TOKEN" \
        "https://api.github.com/repos/${GH_REPO}/releases/tags/${TARGET_TAG}")
else
    RELEASE_INFO=$(curl -s "https://api.github.com/repos/${GH_REPO}/releases/tags/${TARGET_TAG}")
fi

APK_NAME=$(echo "$RELEASE_INFO" | jq -r '.assets[] | select(.name | endswith(".apk")) | .name' | head -1)
if [ -z "$APK_NAME" ] || [ "$APK_NAME" = "null" ]; then
    echo "[✗] Release ${TARGET_TAG} 中没有 APK 资产"
    exit 1
fi
APK_URL=$(echo "$RELEASE_INFO" | jq -r --arg name "$APK_NAME" \
    '.assets[] | select(.name == $name) | .browser_download_url')
echo "    APK 文件: $APK_NAME"

# 3. 下载 APK（国内网络，先直连，失败用 gh-proxy.com）
echo "[3/4] 下载 APK..."
if [ -f "$APK_NAME" ]; then
    echo "    发现已存在的文件 $APK_NAME，跳过下载"
else
    echo "    尝试直连 GitHub..."
    if curl -L --connect-timeout 30 --max-time 300 \
         ${GH_TOKEN:+-H "Authorization: token $GH_TOKEN"} \
         "$APK_URL" -o "$APK_NAME" 2>/dev/null; then
        echo "    直连下载成功"
    else
        echo "    直连失败，使用 gh-proxy.com 国内镜像..."
        if ! curl -L --connect-timeout 30 --max-time 600 \
             ${GH_TOKEN:+-H "Authorization: token $GH_TOKEN"} \
             "https://gh-proxy.com/${APK_URL}" -o "$APK_NAME"; then
            echo "[✗] 下载失败"
            exit 1
        fi
        echo "    镜像下载成功"
    fi
fi
APK_SIZE=$(du -h "$APK_NAME" | awk '{print $1}')
echo "    文件大小: $APK_SIZE"

# 4. 上传到 Gitee Release
echo "[4/4] 上传到 Gitee Release..."
RELEASE_ID=$(curl -s \
    "https://gitee.com/api/v5/repos/${GITEE_REPO}/releases/tags/${TARGET_TAG}?access_token=${GITEE_TOKEN}" | \
    jq -r '.id // empty')

if [ -z "$RELEASE_ID" ] || [ "$RELEASE_ID" = "null" ]; then
    echo "    创建 Gitee Release ${TARGET_TAG}..."
    RESPONSE=$(curl -s -X POST \
        "https://gitee.com/api/v5/repos/${GITEE_REPO}/releases" \
        -H "Content-Type: application/json;charset=UTF-8" \
        -d "{
            \"access_token\": \"${GITEE_TOKEN}\",
            \"tag_name\": \"${TARGET_TAG}\",
            \"name\": \"${TARGET_TAG}\",
            \"body\": \"APK 构建产物（手动同步自 GitHub Release）\",
            \"target_commitish\": \"main\"
        }")
    RELEASE_ID=$(echo "$RESPONSE" | jq -r '.id // empty')
    if [ -z "$RELEASE_ID" ] || [ "$RELEASE_ID" = "null" ]; then
        echo "[✗] 创建 Gitee Release 失败: $RESPONSE"
        exit 1
    fi
fi
echo "    Gitee Release ID: $RELEASE_ID"

echo "    上传 ${APK_NAME} (${APK_SIZE})..."
UPLOAD_RESP=$(curl -s -X POST \
    "https://gitee.com/api/v5/repos/${GITEE_REPO}/releases/${RELEASE_ID}/attach_files" \
    -F "access_token=${GITEE_TOKEN}" \
    -F "file=@${APK_NAME}")

ASSET_URL=$(echo "$UPLOAD_RESP" | jq -r '.browser_download_url // empty')
if [ -n "$ASSET_URL" ]; then
    echo ""
    echo "=========================================="
    echo "  ✅ 同步成功"
    echo "=========================================="
    echo "  Gitee APK: $ASSET_URL"
    echo "  下载页:   https://gitee.com/${GITEE_USER}/one-agent/releases/${TARGET_TAG}"
    echo "=========================================="
else
    echo "[✗] 上传失败: $UPLOAD_RESP"
    exit 1
fi
