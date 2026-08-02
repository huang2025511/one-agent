package com.oneagent

import android.util.Log
import com.linh18nd.flutter_live2d.FlutterLive2dPlugin
import io.flutter.FlutterInjector
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.embedding.engine.FlutterEngineCache
import io.flutter.embedding.engine.FlutterEngineGroup
import io.flutter.embedding.engine.dart.DartExecutor
import io.flutter.plugins.GeneratedPluginRegistrant

class MainActivity : FlutterActivity() {

    companion object {
        private const val TAG = "MainActivity"
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        Log.d(TAG, "🔧 configureFlutterEngine called")
        
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
        
        // 验证主引擎插件注册
        Log.d(TAG, "📋 Main engine plugins count: ${flutterEngine.plugins.size}")
        Log.d(TAG, "📋 Main engine plugins: ${flutterEngine.plugins.map { it.javaClass.simpleName }}")
    }

    /**
     * 预创建悬浮窗 FlutterEngine 并注册所有插件。
     *
     * flutter_overlay_window 的 OverlayService 从 FlutterEngineCache
     * 获取 "myCachedEngine" 引擎来渲染悬浮窗 UI。
     *
     * ⚠️ 关键：GeneratedPluginRegistrant.registerWith() 只注册了部分插件，
     * 不包含 flutter_live2d。
     *
     * FlutterLive2dPlugin 负责在 onAttachedToEngine 中注册
     * PlatformView factory "live2d_view"，没有这个 factory，
     * Live2DView 的 native view 无法创建，whenAttached 超时，
     * 最终只能显示 Canvas fallback 圆形宠物。
     *
     * 因此必须手动实例化并注册 FlutterLive2dPlugin。
     */
    private fun ensureOverlayEngineRegistered() {
        val cachedTag = "myCachedEngine"
        if (FlutterEngineCache.getInstance().get(cachedTag) != null) {
            Log.d(TAG, "overlay engine already cached, skipping")
            return
        }

        Log.d(TAG, "🔨 Creating overlay engine...")
        val engineGroup = FlutterEngineGroup(this)
        val entrypoint = DartExecutor.DartEntrypoint(
            FlutterInjector.instance().flutterLoader().findAppBundlePath(),
            "overlayMain"
        )
        val overlayEngine = engineGroup.createAndRunEngine(this, entrypoint)
        Log.d(TAG, "🔨 Overlay engine created: ${overlayEngine}")

        // 1. 注册 GeneratedPluginRegistrant 中的基础插件
        GeneratedPluginRegistrant.registerWith(overlayEngine)
        Log.d(TAG, "📋 After GeneratedPluginRegistrant, overlay plugins: ${overlayEngine.plugins.map { it.javaClass.simpleName }}")

        // 2. ⚠️ 手动注册 FlutterLive2dPlugin（不在 GeneratedPluginRegistrant 中）
        //    必须在 put 到 cache 之前注册，否则 FlutterOverlayWindowPlugin
        //    检测到缓存引擎后会跳过创建，但引擎缺少 live2d_view factory
        try {
            val live2dPlugin = FlutterLive2dPlugin()
            overlayEngine.plugins.add(live2dPlugin)
            Log.d(TAG, "✅ FlutterLive2dPlugin registered on overlay engine")
            Log.d(TAG, "📋 Final overlay plugins: ${overlayEngine.plugins.map { it.javaClass.simpleName }}")
        } catch (e: Exception) {
            Log.e(TAG, "❌ Failed to register FlutterLive2dPlugin", e)
        }

        FlutterEngineCache.getInstance().put(cachedTag, overlayEngine)
        Log.d(TAG, "✅ overlay engine cached as '$cachedTag'")
    }
}
