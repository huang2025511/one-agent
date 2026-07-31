# Live2D 模型目录

把下载的 Live2D 模型文件夹放到这里。

## 推荐免费模型（Live2D 官方，可商用）

### 1. Haru（推荐，动作+表情+语音齐全）
- 下载页：https://www.live2d.com/en/learn/sample/haru/
- 滚到底部点 "Download" → 选 "For SmartPhone"（手机版，体积小）
- 解压后应该有这些文件：
  ```
  haru/
  ├── haru.model3.json       ← 模型配置（核心）
  ├── haru.moc3              ← 模型数据
  ├── haru.physics3.json     ← 物理演算
  ├── haru.pose3.json        ← 姿势
  ├── haru.cdi3.json         ← 显示辅助
  ├── motions/               ← 动作
  │   ├── idle_01.motion3.json
  │   ├── tap_body_01.motion3.json
  │   └── ...
  ├── expressions/           ← 表情
  │   ├── F01.exp3.json
  │   ├── F02.exp3.json
  │   └── ...
  └── textures/              ← 贴图
      └── texture_00.png
  ```
- 把整个 `haru/` 文件夹复制到本目录（即 `assets/models/haru/`）

### 2. Hiyori（更简洁，适合入门）
- 下载页：https://www.live2d.com/en/learn/sample/momose-hiyori/
- 同样复制到 `assets/models/hiyori/`

### 3. Mark（男生模型）
- 下载页：https://www.live2d.com/en/learn/sample/mark/

## 多模型支持

代码会自动扫描 `assets/models/` 下第一个 `.model3.json`。
如果想指定用某个模型，修改 `lib/services/pet_renderer.dart` 的 `PetRenderer`：

```dart
PetRenderer(
  modelDir: 'assets/models/haru/',
  modelFileName: 'haru.model3.json',
  ...
)
```

## 没有模型也能跑

如果 `assets/models/` 下没有任何 `.model3.json`，
代码会自动 fallback 到 Canvas 自绘动画（圆球宠物），
保证 APP 不会崩溃，可以先跑起来看整体框架。

## 商用授权

Live2D 官方示例模型允许个人和小企业（年销售额 < 1000 万日元）免费商用。
详见：https://www.live2d.com/eula/live2d-free-material-license-agreement_en.html
