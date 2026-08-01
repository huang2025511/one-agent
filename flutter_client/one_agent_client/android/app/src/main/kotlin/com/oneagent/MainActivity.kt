package com.oneagent

import io.flutter.FlutterInjector
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.embedding.engine.FlutterEngineCache
import io.flutter.embedding.engine.FlutterEngineGroup
import io.flutter.embedding.engine.dart.DartExecutor
import io.flutter.plugins.GeneratedPluginRegistrant

class MainActivity : FlutterActivity() {

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        ensureOverlayEngineRegistered()
    }

    /**
     * 预创建悬浮窗 FlutterEngine 并注册所有插件。
     *
     * 问题：flutter_overlay_window 0.4.5 使用
     * FlutterEngineGroup.createAndRunEngine() 创建 overlay 引擎，
     * 但该引擎不会自动注册插件（GeneratedPluginRegistrant）。
     * 导致悬浮窗引擎里没有 FlutterLive2dPlugin，Live2DView 的
     * PlatformView 无法创建，whenAttached 永远不返回，
     * 最终显示 Canvas fallback 圆形 + 错误提示。
     *
     * 修复：在 MainActivity 启动时预创建 overlay 引擎，手动注册所有
     * 插件（包括 flutter_live2d），放入 FlutterEngineCache。
     * flutter_overlay_window 的 FlutterOverlayWindowPlugin 会检测到
     * 缓存已有引擎，直接使用它（跳过自己创建）。
     *
     * 缓存 key "myCachedEngine" 对应 OverlayConstants.CACHED_TAG。
     */
    private fun ensureOverlayEngineRegistered() {
        val cachedTag = "myCachedEngine"
        if (FlutterEngineCache.getInstance().get(cachedTag) != null) return

        val engineGroup = FlutterEngineGroup(this)
        val entrypoint = DartExecutor.DartEntrypoint(
            FlutterInjector.instance().flutterLoader().findAppBundlePath(),
            "overlayMain"
        )
        val overlayEngine = engineGroup.createAndRunEngine(this, entrypoint)
        // 手动注册所有插件（包括 flutter_live2d）
        GeneratedPluginRegistrant.registerWith(overlayEngine)
        FlutterEngineCache.getInstance().put(cachedTag, overlayEngine)
    }
}
