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
        // ⚠️ 必须在 super.configureFlutterEngine() 之前预创建 overlay 引擎！
        //
        // 原因：super.configureFlutterEngine() 会触发主引擎插件注册，
        // 其中 FlutterOverlayWindowPlugin.onAttachedToActivity() 会检查
        // FlutterEngineCache 中是否已有 "myCachedEngine"：
        //   - 若有：跳过创建，直接用缓存的引擎（✅ 我们想要的行为）
        //   - 若无：自己创建一个引擎放入缓存，但不注册插件（❌ 导致
        //     FlutterLive2dPlugin 未注册，Live2DView 无法创建 native view）
        //
        // 所以必须在 super 之前就把注册了插件的 overlay 引擎放入缓存，
        // 这样 FlutterOverlayWindowPlugin 检测到缓存已有引擎就会跳过创建。
        ensureOverlayEngineRegistered()
        super.configureFlutterEngine(flutterEngine)
    }

    /**
     * 预创建悬浮窗 FlutterEngine 并注册所有插件。
     *
     * flutter_overlay_window 的 OverlayService 从 FlutterEngineCache
     * 获取 "myCachedEngine" 引擎来渲染悬浮窗 UI。如果该引擎没有注册
     * FlutterLive2dPlugin，Live2DView 的 PlatformView factory 未注册，
     * native view 无法创建，whenAttached 超时，最终显示 Canvas fallback。
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
