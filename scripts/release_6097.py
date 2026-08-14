#!/usr/bin/env python3
"""创建 Gitee release app-v6097 并上传 APK（带 429 限流重试）"""
import json
import time
import urllib.request
import uuid

TOKEN = "de4eaf21cffbd8b3b9c3522de99b879b"
OWNER_REPO = "huang20260511/one-agent"
API = f"https://gitee.com/api/v5/repos/{OWNER_REPO}"
APK = "/tmp/l2d/v6097.apk"

BODY = """## 修复与增强

### 1. 聊天回复不显示（根因修复）
页面把回复正文追加在 thinking 文本后面，而气泡 overflow:hidden 只显示开头——回复全部被裁掉，表现为"一直思考、没有回复"。
现在回复与 thinking 分离：正文到达时清空思考文本、独立累积流式内容，气泡自动滚动到最新，加高到 55% 可滚动。

### 2. 对话窗口改为点击唤出
输入框不再常驻遮挡宠物。点击宠物切换显示/隐藏对话条；点中模型本体还有随机动作 + 随机表情 + 互动回应。

### 3. 自动行为系统（宠物活起来）
- **随机闲晃**：每 12~22 秒从 Idle 组 9 个动作中随机播放（说话时不打断）
- **随机动作组**：问候/说话/点击互动改为从全部动作组随机选择，不再固定单一动作
- **视线跟随**：手指滑过时宠物看向触点
- 模型自带呼吸、眨眼、口型同步持续生效"""


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
    "tag_name": "app-v6097",
    "name": "One-Agent App v6097 (1.0.99)",
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
