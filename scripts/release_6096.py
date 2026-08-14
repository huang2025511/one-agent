#!/usr/bin/env python3
"""创建 Gitee release app-v6096 并上传 APK（带 429 限流重试）"""
import json
import time
import urllib.request
import uuid

TOKEN = "de4eaf21cffbd8b3b9c3522de99b879b"
OWNER_REPO = "huang20260511/one-agent"
API = f"https://gitee.com/api/v5/repos/{OWNER_REPO}"
APK = "/tmp/l2d/v6096.apk"

BODY = """## 修复

**桌宠"库能加载但宠物不显示"根因修复**

### 根因
悬浮窗 `addView()` 后立即 `loadUrl()`，本地资源经 WebViewAssetLoader 加载极快，页面脚本执行时 WebView 首次 layout 往往未完成，`window.innerWidth/innerHeight` 为 0 → WebGL 画布被创建为 **0×0** → 模型加载成功也完全不可见（且无任何报错）。

### 修复
1. **视口等待**：视口尺寸为 0 时轮询等待（最长 5 秒），拿到非零尺寸后再创建渲染器
2. **加载排队**：渲染器未就绪时模型加载请求自动排队，就绪后立即执行（此前 app 为 null 会抛 TypeError，气泡卡在"加载模型中"）
3. **数值防护**：模型 bounds 为 0 时 scale 会变 Infinity 导致不渲染，已加防护
4. **渲染诊断**：渲染器就绪、模型 fit 缩放、排队等关键节点全部上报 logcat

### 验证
真实 Chromium（280×360 视口，与悬浮窗一致）端到端验证：模型加载成功、fit 正常（scale=0.07，原始高约 4000 单位）、截图可见完整人物轮廓。"""


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
    "tag_name": "app-v6096",
    "name": "One-Agent App v6096 (1.0.98)",
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
