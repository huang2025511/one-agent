# 桌宠模块（Live2D + 悬浮窗）

本模块在 one-agent Flutter 客户端基础上，新增了**安卓桌面悬浮窗 + Live2D 桌宠**功能。
宠物会浮在所有 APP 上方，可拖拽、可点击、可聊天，对话通过 one-agent 后端的 SSE 流式接口完成。

## 架构

```
主 APP (Flutter)
  └── 点击 FAB 按钮 → 启动悬浮窗
                        ↓
悬浮窗 (独立 Flutter 引擎)
  ├── PetRenderer
  │   ├── 优先：Live2DView（flutter_live2d，加载 assets/models/ 下的模型）
  │   └── 兜底：Canvas 自绘（呼吸/眨眼/嘴型/5 种表情）
  ├── 聊天气泡（打字机效果）
  └── 输入框 → 调 one-agent /api/chat/stream (SSE)
                ↓
            收到流式回复
                ↓
            宠物切换 talking 模式 + 嘴型同步 + 气泡显示
                ↓
            回复结束 → 3 秒后回 idle
```

## 文件清单

| 文件 | 作用 |
|---|---|
| `lib/services/pet_renderer.dart` | 宠物渲染（Live2D 优先 + Canvas 兜底） |
| `lib/services/overlay_pet_service.dart` | 悬浮窗管理（权限/启停/双向通信） |
| `lib/screens/overlay_pet_screen.dart` | 悬浮窗内 UI（宠物+气泡+输入框+SSE） |
| `lib/providers/pet_provider.dart` | 桌宠状态管理（Riverpod） |
| `android/app/src/main/AndroidManifest.xml` | 加 SYSTEM_ALERT_WINDOW 权限 |
| `android/app/src/main/res/values/styles.xml` | 加 Theme.OverlayPet 透明主题 |
| `assets/models/` | Live2D 模型目录（用户自行下载放入） |

## 快速开始

### 1. 下载 Live2D 模型（可选但推荐）

详见 [assets/models/README.md](assets/models/README.md)。
推荐用官方 Haru 模型：https://www.live2d.com/en/learn/sample/haru/

下载后解压到 `assets/models/haru/`，确保目录结构：
```
assets/models/haru/haru.model3.json
assets/models/haru/haru.moc3
assets/models/haru/motions/...
assets/models/haru/expressions/...
assets/models/haru/textures/...
```

> 没有模型也能跑，会自动用 Canvas 自绘圆球宠物。

### 2. 安装依赖

```bash
cd flutter_client/one_agent_client
flutter pub get
```

### 3. 运行

确保 one-agent 后端在运行（默认端口 18792），然后：

```bash
flutter run                    # 调试运行
# 或打包 APK
flutter build apk --release
```

### 4. 使用

1. 在 APP 设置里配置 one-agent 服务器地址（如 `http://192.168.1.100:18792`）
2. 点右下角的**宠物图标 FAB**
3. 第一次会弹"显示在其他应用上层"权限，授权
4. 桌面会出现会动的宠物
5. 点宠物 → 弹输入框 → 输入消息 → 宠物会"思考"然后"说话"

## Live2D 表情/动作映射

代码会根据聊天状态自动驱动 Live2D：

| 聊天状态 | mood | Live2D 驱动 |
|---|---|---|
| 待机 | idle | 启动 Idle 动作组 |
| 思考中 | thinking | 切换表情 + 问号气泡 |
| 说话中 | talking | `ParamMouthOpenY` 实时驱动嘴型 |
| 回复完成 | happy | 切换开心表情 + Tap 动作 |
| 睡觉 | sleeping | `ParamEyeLOpen/ROpen=0` 闭眼 |

> 不同模型的表情索引和动作组名可能不同，代码做了容错（找不到就忽略）。
> 如果表情对不上，编辑 `pet_renderer.dart` 的 `_moodToExpressionIndex` 和 `_moodToMotionGroup`。

## 常见问题

### Q: Live2D 模型加载失败？
检查：
1. `pubspec.yaml` 的 `flutter.assets` 是否包含 `- assets/models/`
2. 模型目录是否含 `.model3.json` + `.moc3` + `textures/`
3. 看 logcat 是否有 `LOAD_FAILED` 错误
4. `.moc3` 版本是否在 3.0-5.3 之间（flutter_live2d 支持范围）

### Q: 悬浮窗不显示？
1. 检查是否授权了"显示在其他应用上层"权限
2. 国产 ROM（小米/华为/OPPO）可能需要在电池优化里加白名单
3. 看 logcat 是否有 `SYSTEM_ALERT_WINDOW` 相关错误

### Q: 聊天不回复？
1. 确认 one-agent 后端在运行
2. 确认 APP 设置里的 baseUrl 正确（手机访问电脑用局域网 IP，不是 127.0.0.1）
3. 确认 `/api/chat/stream` 接口可访问

## 后续规划

- [ ] TTS 语音合成 + 嘴型实时同步（按音频音量驱动 `ParamMouthOpenY`）
- [ ] one-agent 加 pet_action 接口，让 LLM 主动控制宠物表情/动作
- [ ] 长期记忆驱动"宠物记得你"
- [ ] scheduler 定时主动问候
- [ ] 图片生成模型 → 自动生成 Live2D 形象
