package com.oneagent

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.PixelFormat
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import org.json.JSONObject
import java.io.BufferedReader
import java.io.File
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLDecoder

/**
 * 原生桌宠悬浮窗服务。
 *
 * 采用开源社区（星尘 Stradust / Live2DViewerEX 悬浮窗版）验证过的方案：
 * 系统级悬浮窗 + 原生 WebView 渲染 Live2D（pixi-live2d-display），
 * 彻底绕开 Flutter PlatformView 无法在系统悬浮窗渲染的问题。
 *
 * 功能：Live2D 模型显示、气泡消息、文字聊天（SSE 流式）、
 * 点击模型互动、自由拖动、关闭按钮。
 */
class PetOverlayService : Service() {

    companion object {
        private const val TAG = "PetOverlayService"
        private const val NOTIF_ID = 10086
        private const val CHANNEL_ID = "pet_overlay_channel"

        var instance: PetOverlayService? = null
            private set
    }

    private lateinit var wm: WindowManager
    private val mainHandler = Handler(Looper.getMainLooper())

    private var overlayView: DragLayout? = null
    private var webView: WebView? = null
    private var params: WindowManager.LayoutParams? = null

    private var baseUrl: String = ""
    private var apiKey: String = ""
    private var sessionId: String? = null
    private var modelUrl: String = ""

    private var webDir: File? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        wm = getSystemService(Context.WINDOW_SERVICE) as WindowManager
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        baseUrl = intent?.getStringExtra("baseUrl") ?: baseUrl
        apiKey = intent?.getStringExtra("apiKey") ?: apiKey

        val updateOnly = intent?.getBooleanExtra("updateOnly", false) ?: false
        val mp = intent?.getStringExtra("modelPath")
        val mf = intent?.getStringExtra("modelFileName")

        if (updateOnly) {
            // 仅切换模型（悬浮窗已在运行）。
            // 注意：updateModel 通过 startForegroundService 投递，
            // 必须调用 startForeground，否则服务未在前台时抛
            // ForegroundServiceDidNotStartInTimeException（已在前台时重复调用无害）。
            startForegroundCompat()
            modelUrl = resolveModelUrl(mp, mf)
            if (overlayView != null) {
                mainHandler.post { loadModel(modelUrl) }
            } else {
                // 服务存活但悬浮窗视图不存在（异常边缘情况）→ 重建
                mainHandler.post { showOverlay() }
            }
            return START_NOT_STICKY
        }

        if (!mp.isNullOrEmpty() && !mf.isNullOrEmpty()) {
            modelUrl = resolveModelUrl(mp, mf)
        }

        startForegroundCompat()
        if (overlayView == null) {
            mainHandler.post { showOverlay() }
        } else if (modelUrl.isNotEmpty()) {
            mainHandler.post { loadModel(modelUrl) }
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        instance = null
        try {
            overlayView?.let { wm.removeView(it) }
        } catch (e: Exception) {
            Log.w(TAG, "removeView: $e")
        }
        overlayView = null
        webView?.destroy()
        webView = null
        stopForeground(true)
        super.onDestroy()
    }

    /** 解析模型 URL：传了路径用导入模型，否则用内置 hiyori */
    private fun resolveModelUrl(mp: String?, mf: String?): String {
        return if (!mp.isNullOrEmpty() && !mf.isNullOrEmpty()) {
            "file://" + File(mp, mf).absolutePath
        } else {
            val dir = webDir ?: prepareWebDir()
            "file://" + File(dir, "models/hiyori/Hiyori.model3.json").absolutePath
        }
    }

    // ── 前台服务通知 ────────────────────────────────────────
    private fun startForegroundCompat() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                val nm = getSystemService(NotificationManager::class.java)
                if (nm.getNotificationChannel(CHANNEL_ID) == null) {
                    nm.createNotificationChannel(NotificationChannel(
                        CHANNEL_ID, "桌宠运行中",
                        NotificationManager.IMPORTANCE_LOW))
                }
            }
            @Suppress("DEPRECATION")
            val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                Notification.Builder(this, CHANNEL_ID)
            } else {
                Notification.Builder(this)
            }
            val notif = builder
                .setContentTitle("One-Agent 桌宠")
                .setContentText("桌宠运行中")
                .setSmallIcon(android.R.drawable.ic_menu_compass)
                .setOngoing(true)
                .build()
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTIF_ID, notif,
                    android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
            } else {
                startForeground(NOTIF_ID, notif)
            }
        } catch (e: Exception) {
            Log.e(TAG, "startForeground failed: $e")
        }
    }

    private fun dp(v: Float): Int =
        (v * resources.displayMetrics.density).toInt()

    // ── 悬浮窗视图 ─────────────────────────────────────────
    private fun showOverlay() {
        if (overlayView != null) return

        // 解压 web 资源与内置模型到内部存储
        webDir = prepareWebDir()

        val layout = DragLayout(this)
        layout.onDrag = { dx, dy ->
            params?.let { p ->
                p.x += dx.toInt()
                p.y += dy.toInt()
                try { wm.updateViewLayout(layout, p) } catch (_: Exception) {}
            }
        }

        val wv = WebView(this)
        webView = wv
        wv.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            allowFileAccess = true
            allowContentAccess = true
            mediaPlaybackRequiresUserGesture = false
            @Suppress("DEPRECATION")
            allowFileAccessFromFileURLs = true
            @Suppress("DEPRECATION")
            allowUniversalAccessFromFileURLs = true
        }
        wv.setBackgroundColor(Color.TRANSPARENT)
        wv.setLayerType(View.LAYER_TYPE_HARDWARE, null)
        wv.addJavascriptInterface(Bridge(), "PetBridge")
        wv.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                if (modelUrl.isNotEmpty()) loadModel(modelUrl)
            }

            // 拦截 file:// 请求直接读本地文件，保证 WebView 能加载模型资源
            override fun shouldInterceptRequest(
                view: WebView?, request: WebResourceRequest
            ): WebResourceResponse? {
                val url = request.url.toString()
                if (url.startsWith("file://")) {
                    val path = try {
                        URLDecoder.decode(url.removePrefix("file://"), "UTF-8")
                    } catch (e: Exception) {
                        url.removePrefix("file://")
                    }
                    val f = File(path)
                    if (f.exists() && f.isFile) {
                        return WebResourceResponse(mimeOf(f.name), "UTF-8", f.inputStream())
                    }
                }
                return super.shouldInterceptRequest(view, request)
            }
        }

        layout.addView(wv, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT))

        val w = dp(280f)
        val h = dp(360f)
        // TYPE_APPLICATION_OVERLAY 需要 API 26+；更低版本用 TYPE_PHONE
        val windowType = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        } else {
            @Suppress("DEPRECATION")
            WindowManager.LayoutParams.TYPE_PHONE
        }
        val p = WindowManager.LayoutParams(
            w, h,
            windowType,
            0, // 不加 NOT_FOCUSABLE：允许 WebView 输入框弹键盘
            PixelFormat.TRANSLUCENT
        )
        p.gravity = Gravity.TOP or Gravity.START
        // 初始位置：屏幕右侧中部
        val dm = resources.displayMetrics
        p.x = (dm.widthPixels - w - dp(8f)).coerceAtLeast(0)
        p.y = ((dm.heightPixels - h) / 2).coerceAtLeast(0)
        params = p

        overlayView = layout
        try {
            wm.addView(layout, p)
        } catch (e: Exception) {
            Log.e(TAG, "addView failed: $e")
            overlayView = null
            stopSelf()
            return
        }

        wv.loadUrl("file://" + File(webDir, "index.html").absolutePath)
    }

    private fun mimeOf(name: String): String = when {
        name.endsWith(".js") -> "application/javascript"
        name.endsWith(".json") -> "application/json"
        name.endsWith(".html") -> "text/html"
        name.endsWith(".png") -> "image/png"
        name.endsWith(".jpg") || name.endsWith(".jpeg") -> "image/jpeg"
        name.endsWith(".moc3") -> "application/octet-stream"
        else -> "application/octet-stream"
    }

    /**
     * 把 Flutter assets（flutter_assets/ 前缀）复制到内部存储。
     * Flutter 打包的资源位于 APK 的 assets/flutter_assets/ 下。
     *
     * 用 versionCode 做缓存标记：应用升级后自动覆盖旧的 web 资源，
     * 避免 index.html 更新不生效。
     */
    private fun prepareWebDir(): File {
        val dir = File(filesDir, "live2d_web")
        val versionMarker = File(dir, ".web_version")
        val currentVersion = try {
            val pi = packageManager.getPackageInfo(packageName, 0)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                pi.longVersionCode.toString()
            } else {
                @Suppress("DEPRECATION")
                pi.versionCode.toString()
            }
        } catch (e: Exception) { "0" }
        val cachedVersion = try { versionMarker.readText() } catch (e: Exception) { "" }

        if (!File(dir, "index.html").exists() || cachedVersion != currentVersion) {
            dir.deleteRecursively()
            copyAssetDir("flutter_assets/assets/live2d_web", dir)
            try { versionMarker.writeText(currentVersion) } catch (_: Exception) {}
        }
        val modelMarker = File(dir, "models/hiyori/Hiyori.model3.json")
        if (!modelMarker.exists()) {
            copyAssetDir("flutter_assets/assets/models/hiyori", File(dir, "models/hiyori"))
        }
        return dir
    }

    private fun copyAssetDir(assetPath: String, dest: File) {
        val am = assets
        val entries = am.list(assetPath)
        if (entries == null || entries.isEmpty()) {
            // 是单个文件
            try {
                am.open(assetPath).use { input ->
                    dest.parentFile?.mkdirs()
                    dest.outputStream().use { out -> input.copyTo(out) }
                }
            } catch (e: Exception) {
                Log.w(TAG, "copy asset file failed: $assetPath $e")
            }
            return
        }
        dest.mkdirs()
        for (name in entries) {
            copyAssetDir("$assetPath/$name", File(dest, name))
        }
    }

    private fun loadModel(url: String) {
        modelUrl = url
        // 守卫：页面脚本未就绪时 petLoadModel 不存在，避免 JS 报错；
        // 脚本就绪后 JS 会主动调用 PetBridge.onReady() 触发加载。
        webView?.evaluateJavascript(
            "if (window.petLoadModel) window.petLoadModel(${JSONObject.quote(url)})", null)
    }

    // ── JS 桥 ──────────────────────────────────────────────
    inner class Bridge {
        @JavascriptInterface
        fun onReady() {
            mainHandler.post {
                if (modelUrl.isEmpty()) {
                    modelUrl = resolveModelUrl(null, null)
                }
                loadModel(modelUrl)
            }
        }

        @JavascriptInterface
        fun onModelLoaded(ok: Boolean, info: String) {
            Log.i(TAG, "model loaded: ok=$ok info=$info")
        }

        @JavascriptInterface
        fun onClose() {
            mainHandler.post { stopSelf() }
        }

        @JavascriptInterface
        fun onSend(json: String) {
            Thread { doChat(json) }.start()
        }
    }

    // ── SSE 聊天（与 Flutter 端 sse_client 协议一致）────────
    private fun doChat(json: String) {
        val req = try { JSONObject(json) } catch (e: Exception) { return }
        val text = req.optString("text", "")
        if (text.isEmpty()) return
        val sid = req.optString("session_id", sessionId ?: "")

        val cleanBase = baseUrl.trimEnd('/')
        if (cleanBase.isEmpty()) {
            pushEvent("""{"error":"未配置服务器地址，请在设置中填写","done":true}""")
            return
        }
        try {
            val url = URL("$cleanBase/api/chat/stream")
            val conn = url.openConnection() as HttpURLConnection
            conn.requestMethod = "POST"
            conn.connectTimeout = 15000
            conn.readTimeout = 120000
            conn.doOutput = true
            conn.setRequestProperty("Content-Type", "application/json")
            conn.setRequestProperty("Accept", "text/event-stream")
            if (apiKey.isNotEmpty()) conn.setRequestProperty("X-API-Key", apiKey)

            val body = JSONObject().apply {
                put("text", text)
                if (sid.isNotEmpty()) put("session_id", sid)
            }
            conn.outputStream.use { it.write(body.toString().toByteArray()) }

            if (conn.responseCode != 200) {
                pushEvent("""{"error":"服务器错误: ${conn.responseCode}","done":true}""")
                return
            }

            BufferedReader(InputStreamReader(conn.inputStream, Charsets.UTF_8)).use { br ->
                val sb = StringBuilder()
                var line: String?
                while (br.readLine().also { line = it } != null) {
                    val l = line!!
                    if (l.isEmpty()) {
                        if (sb.isNotEmpty()) {
                            handleSseBlock(sb.toString())
                            sb.setLength(0)
                        }
                    } else if (l.startsWith("data:")) {
                        if (sb.isNotEmpty()) sb.append('\n')
                        sb.append(l.removePrefix("data:").trimStart())
                    }
                }
                if (sb.isNotEmpty()) handleSseBlock(sb.toString())
            }
        } catch (e: Exception) {
            Log.e(TAG, "chat error: $e")
            pushEvent("""{"error":"连接失败: ${e.message}","done":true}""")
        }
    }

    private fun handleSseBlock(data: String) {
        try {
            val j = JSONObject(data)
            j.optString("session_id").takeIf { it.isNotEmpty() }?.let { sessionId = it }
        } catch (_: Exception) {}
        pushEvent(data)
    }

    private fun pushEvent(dataJson: String) {
        mainHandler.post {
            webView?.evaluateJavascript(
                "window.onChatEvent(${JSONObject.quote(dataJson)})", null)
        }
    }

    // ── 可拖动容器：轻点穿透给 WebView，滑动拖动移动窗口 ─────
    class DragLayout(context: Context) : FrameLayout(context) {
        var onDrag: ((Float, Float) -> Unit)? = null
        private var downX = 0f
        private var downY = 0f
        private var dragging = false

        override fun onInterceptTouchEvent(ev: MotionEvent): Boolean {
            when (ev.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downX = ev.rawX; downY = ev.rawY; dragging = false
                    return false
                }
                MotionEvent.ACTION_MOVE -> {
                    if (!dragging &&
                        (Math.abs(ev.rawX - downX) > 20 || Math.abs(ev.rawY - downY) > 20)) {
                        dragging = true
                        return true
                    }
                }
            }
            return dragging
        }

        override fun onTouchEvent(ev: MotionEvent): Boolean {
            when (ev.actionMasked) {
                MotionEvent.ACTION_MOVE -> {
                    if (dragging) {
                        onDrag?.invoke(ev.rawX - downX, ev.rawY - downY)
                        downX = ev.rawX; downY = ev.rawY
                        return true
                    }
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> dragging = false
            }
            return dragging
        }
    }
}
