#!/usr/bin/env python3
"""创建 Gitee release app-v6095 并上传 APK（带 429 限流重试）"""
import json
import time
import urllib.request
import uuid

TOKEN = "de4eaf21cffbd8b3b9c3522de99b879b"
OWNER_REPO = "huang20260511/one-agent"
API = f"https://gitee.com/api/v5/repos/{OWNER_REPO}"
APK = "/tmp/l2d/v6095.apk"

BODY = """## 修复

**检查更新误判"已是最新版本"根因修复**

### 问题
v4095 的 APK versionCode 被误标为 4095，而历史版本（v4092/4093/4094）的实际 versionCode 是 6092/6093/6094。导致：
- 设备 buildNumber=6094 与服务端 4095 比较，6094 < 4095 不成立 → 误判"已是最新版本"
- 即使强制下载，Android 也拒绝覆盖安装（versionCode 降级）

### 修复
本版本 versionCode=6095（恢复 6xxx 正确序列），所有存量设备（6092/6093/6094）都能：
- 正确收到更新提示（6xxx < 6095）
- 正常覆盖安装（6095 > 6094）

同时包含 v4095 的桌宠渲染库修复（ES5 转译 + Cubism4 专用版 + polyfill 内联）。"""


def req(url, data=None, headers=None, retry=5):
    for i in range(retry):
        try:
            r = urllib.request.Request(url, data=data, headers=headers or {})
            with urllib.request.urlopen(r, timeout=120) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            if e.code == 429 and i < retry - 1:
                print(f"  429 限流，{30 * (i + 1)}s 后重试...")
                time.sleep(30 * (i + 1))
                continue
            raise RuntimeError(f"HTTP {e.code}: {body[:300]}")
    raise RuntimeError("retries exhausted")


payload = json.dumps({
    "access_token": TOKEN,
    "tag_name": "app-v6095",
    "name": "One-Agent App v6095 (1.0.97)",
    "body": BODY,
    "target_commitish": "main",
}).encode("utf-8")

status, raw = req(
    f"{API}/releases",
    data=payload,
    headers={"Content-Type": "application/json;charset=UTF-8"},
)
rel = json.loads(raw)
print(f"release 创建: id={rel['id']} tag={rel['tag_name']}")

boundary = uuid.uuid4().hex
with open(APK, "rb") as f:
    apk_bytes = f.read()

parts = []
parts.append(
    f"--{boundary}\r\nContent-Disposition: form-data; name=\"access_token\"\r\n\r\n{TOKEN}\r\n".encode()
)
parts.append(
    f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"app-arm64-v8a-release.apk\"\r\n"
    f"Content-Type: application/vnd.android.package-archive\r\n\r\n".encode()
    + apk_bytes + f"\r\n--{boundary}--\r\n".encode()
)

status, raw = req(
    f"{API}/releases/{rel['id']}/attach_files",
    data=b"".join(parts),
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
)
att = json.loads(raw)
print(f"附件: {att['name']} ({att['size']} bytes)")

status, raw = req(f"{API}/releases?per_page=1&direction=desc&sort=created_at")
latest = json.loads(raw)[0]
print(f"校验: tag={latest['tag_name']}")
for a in latest.get("assets", []):
    if a["name"].endswith(".apk"):
        print(f"  下载: {a['browser_download_url']}")
