package com.oneagent

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.PixelFormat
import android.net.Uri
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
import androidx.webkit.WebViewAssetLoader
import org.json.JSONObject
import java.io.BufferedReader
import java.io.File
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

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

        // WebViewAssetLoader 虚拟域名（androidx.webkit 官方方案）。
        // Chromium 对 file:// 的 XHR/fetch 有严格同源限制，会导致
        // Live2D 模型资源加载被 CORS 拦截（"模型加载中"卡死的根因）。
        // 走 https 虚拟域名后页面与模型资源同源，无任何限制。
        private const val WEB_BASE = "https://appassets.androidplatform.net/live2d"
        private const val MODELS_BASE = "https://appassets.androidplatform.net/models"

        // 基准尺寸与缩放范围（ratio 相对 280x360）
        private const val BASE_W = 280f
        private const val BASE_H = 360f
        private const val MIN_SCALE = 0.4f
        private const val MAX_SCALE = 2.5f
        private val SIZE_NAMES = arrayOf("小", "中", "大")
        private const val PREFS = "pet_overlay"
        private const val KEY_SCALE = "scale_ratio"

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

    // 当前 SSE 聊天连接（新请求到达时断开旧连接，防止泄漏与事件串流）
    @Volatile private var chatConn: HttpURLConnection? = null

    // 聊天代际：每次新请求递增。旧线程断连生效前可能读到缓冲中的陈旧 SSE
    // 块，用代际判断丢弃，避免旧回复的事件混入新请求的气泡
    @Volatile private var chatGen = 0L

    // 聊天执行器：单线程串行 + 无界队列。替代之前每次发送裸 new Thread ——
    // 狂点发送会堆积线程（每条都要建 TCP 连接才被代际保护踢出）。
    // 单线程保证同一时刻至多一条 SSE 连接，天然配合代际接管逻辑。
    private val chatExecutor: ExecutorService = Executors.newSingleThreadExecutor()

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

        // 始终解析模型 URL：无导入模型时回退内置 Hiyori（否则宠物窗口空白）
        modelUrl = resolveModelUrl(mp, mf)

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
        // 清除所有待执行任务（如 loadModel/showOverlay 的 post），
        // 防止 onDestroy 后仍向已销毁的 WebView 派发操作
        mainHandler.removeCallbacksAndMessages(null)
        // 关闭聊天执行器：丢弃排队任务并中断进行中的 SSE 读取
        chatExecutor.shutdownNow()
        // 断开进行中的 SSE 连接，释放线程与socket
        try { chatConn?.disconnect() } catch (_: Exception) {}
        chatConn = null
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
        if (!mp.isNullOrEmpty() && !mf.isNullOrEmpty()) {
            // 导入模型位于 app_flutter/live2d_models/<名>/（path_provider 的文档目录）
            val modelsRoot = File(getDir("flutter", Context.MODE_PRIVATE), "live2d_models")
            try {
                val abs = File(mp).canonicalFile
                val rootPath = modelsRoot.canonicalPath
                if (abs.path.startsWith("$rootPath/")) {
                    val rel = abs.path.substring(rootPath.length + 1) + "/" + mf
                    val encoded = rel.split('/').joinToString("/") { Uri.encode(it) }
                    return "$MODELS_BASE/$encoded"
                }
            } catch (e: Exception) {
                Log.w(TAG, "resolve model url: $e")
            }
        }
        webDir ?: prepareWebDir()
        return "$WEB_BASE/models/hiyori/Hiyori.model3.json"
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
        }
        wv.setBackgroundColor(Color.TRANSPARENT)
        wv.setLayerType(View.LAYER_TYPE_HARDWARE, null)
        wv.addJavascriptInterface(Bridge(), "PetBridge")

        // WebViewAssetLoader：本地文件通过 https 虚拟域名提供（Google 官方方案）
        // - /live2d/* → filesDir/live2d_web（页面 + 内置 Hiyori 模型）
        // - /models/* → app_flutter/live2d_models（用户导入的模型）
        webDir = prepareWebDir()
        val webRoot = webDir!!
        val modelsRoot = File(getDir("flutter", Context.MODE_PRIVATE), "live2d_models")
        val assetLoader = WebViewAssetLoader.Builder()
            .addPathHandler("/live2d/", WebViewAssetLoader.PathHandler { path ->
                serveFile(webRoot, path)
            })
            .addPathHandler("/models/", WebViewAssetLoader.PathHandler { path ->
                serveFile(modelsRoot, path)
            })
            .build()

        wv.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                if (modelUrl.isNotEmpty()) loadModel(modelUrl)
            }

            override fun shouldInterceptRequest(
                view: WebView?, request: WebResourceRequest
            ): WebResourceResponse? {
                val resp = assetLoader.shouldInterceptRequest(request.url)
                if (resp != null) return resp
                return super.shouldInterceptRequest(view, request)
            }
        }

        // 把页面 console 输出转到 logcat，方便排查模型加载问题
        wv.webChromeClient = object : android.webkit.WebChromeClient() {
            override fun onConsoleMessage(msg: android.webkit.ConsoleMessage): Boolean {
                Log.d("PetWeb", "[${msg.lineNumber()}] ${msg.message()}")
                return true
            }
        }

        layout.addView(wv, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT))

        // 初始尺寸：读取持久化的缩放比例（默认 1.0）
        val ratio = scaleRatio()
        val w = dp(BASE_W * ratio)
        val h = dp(BASE_H * ratio)
        // TYPE_APPLICATION_OVERLAY 需要 API 26+；更低版本用 TYPE_PHONE
        val windowType = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        } else {
            @Suppress("DEPRECATION")
            WindowManager.LayoutParams.TYPE_PHONE
        }
        // 关键修复（模型不显示的根因）：Service 通过 WindowManager 添加的
        // 窗口默认【不做硬件加速】（只有 Activity 窗口默认开启），必须在
        // addView 前显式设置 FLAG_HARDWARE_ACCELERATED。否则 WebView 的
        // WebGL 上下文创建失败，Live2D 模型永远渲染不出来。
        // 关键修复（挡全屏问题）：不加 FLAG_NOT_TOUCH_MODAL 时，可聚焦窗口
        // 会消费【所有】指针事件（包括窗口外的），导致宠物悬浮窗挡住整个屏幕。
        // 加上 NOT_TOUCH_MODAL：窗口外触摸透传给下层应用；窗口仍可聚焦，
        // 输入框能弹键盘。WATCH_OUTSIDE_TOUCH 让窗口感知外部触摸。
        val p = WindowManager.LayoutParams(
            w, h,
            windowType,
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or
                WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH or
                WindowManager.LayoutParams.FLAG_HARDWARE_ACCELERATED,
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

        wv.loadUrl("$WEB_BASE/index.html")
    }

    // ── 尺寸调整（双击循环档位 / 双指捏合连续缩放）──────────
    private fun scaleRatio(): Float =
        getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getFloat(KEY_SCALE, 1.0f).coerceIn(MIN_SCALE, MAX_SCALE)

    /** 应用缩放比例并持久化。关键修复：尺寸变化后把窗口位置
     *  clamp 回屏幕内——否则窗口放大时底部（含发送按钮）会伸出
     *  屏幕外看不到 */
    private fun applyScale(ratio: Float, notify: Boolean) {
        val r = ratio.coerceIn(MIN_SCALE, MAX_SCALE)
        getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putFloat(KEY_SCALE, r).apply()
        val w = dp(BASE_W * r)
        val h = dp(BASE_H * r)
        params?.let { p ->
            p.width = w
            p.height = h
            val dm = resources.displayMetrics
            p.x = p.x.coerceIn(0, (dm.widthPixels - w).coerceAtLeast(0))
            p.y = p.y.coerceIn(0, (dm.heightPixels - h).coerceAtLeast(0))
            try {
                wm.updateViewLayout(overlayView, p)
                Log.i(TAG, "scale -> ${"%.2f".format(r)} (${w}x${h}px)")
            } catch (e: Exception) {
                Log.w(TAG, "updateViewLayout: $e")
            }
        }
        if (notify) {
            webView?.evaluateJavascript(
                "window.petBubble && window.petBubble('缩放: ${"%.0f".format(r * 100)}%', false)", null)
        }
    }

    /** 双击：循环切换三档（0.7 / 1.0 / 1.3） */
    private fun cycleSize() {
        val gears = floatArrayOf(0.714f, 1.0f, 1.286f)
        val cur = scaleRatio()
        val next = gears.firstOrNull { it > cur + 0.05f } ?: gears[0]
        applyScale(next, true)
    }

    /** 从指定根目录安全地提供一个文件（防目录穿越） */
    private fun serveFile(root: File, path: String?): WebResourceResponse? {
        if (path.isNullOrEmpty()) return null
        return try {
            val rootPath = root.canonicalPath
            val f = File(root, path).canonicalFile
            if (!f.path.startsWith("$rootPath/") || !f.isFile) return null
            WebResourceResponse(mimeOf(f.name), null, f.inputStream())
        } catch (e: Exception) {
            null
        }
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
        fun log(msg: String) {
            Log.d(TAG, "[js] $msg")
        }

        @JavascriptInterface
        fun onClose() {
            mainHandler.post { stopSelf() }
        }

        /** 双击宠物：循环切换尺寸档位（小/中/大） */
        @JavascriptInterface
        fun cycleSize() {
            mainHandler.post { this@PetOverlayService.cycleSize() }
        }

        /** 双指捏合：按距离比例连续缩放（页面节流后调用） */
        @JavascriptInterface
        fun scaleBy(delta: Double) {
            mainHandler.post {
                applyScale(scaleRatio() * delta.toFloat(), false)
            }
        }

        @JavascriptInterface
        fun onSend(json: String) {
            chatExecutor.execute { doChat(json) }
        }
    }

    // ── SSE 聊天（与 Flutter 端 sse_client 协议一致）────────
    private fun doChat(json: String) {
        val req = try { JSONObject(json) } catch (e: Exception) { return }
        val text = req.optString("text", "")
        if (text.isEmpty()) return
        val sid = req.optString("session_id", sessionId ?: "")

        // 串扰防护：进入即接管代际。旧线程此后所有读取/推送都因代际
        // 不匹配被丢弃，不会把陈旧事件混入新请求的气泡。
        val gen = ++chatGen

        val cleanBase = baseUrl.trimEnd('/')
        if (cleanBase.isEmpty()) {
            if (chatGen == gen) pushEvent(JSONObject()
                .put("error", "未配置服务器地址，请在设置中填写")
                .put("done", true).toString())
            return
        }
        var conn: HttpURLConnection? = null
        try {
            // 新请求到达时断开上一条未完成的 SSE 连接，
            // 避免两条流并发向同一个气泡推送事件
            try { chatConn?.disconnect() } catch (_: Exception) {}
            val url = URL("$cleanBase/api/chat/stream")
            conn = url.openConnection() as HttpURLConnection
            chatConn = conn
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
                // 用 JSONObject 构造错误：responseCode 拼接虽安全，统一走
                // 转义路径避免将来改动引入未转义字符破坏 JSON
                if (chatGen == gen) pushEvent(JSONObject()
                    .put("error", "服务器错误: ${conn.responseCode}")
                    .put("done", true).toString())
                return
            }

            BufferedReader(InputStreamReader(conn.inputStream, Charsets.UTF_8)).use { br ->
                val sb = StringBuilder()
                var line: String?
                while (br.readLine().also { line = it } != null) {
                    // 已被新请求接管：丢弃所有陈旧数据，退出循环
                    if (chatGen != gen) break
                    val l = line!!
                    if (l.isEmpty()) {
                        if (sb.isNotEmpty()) {
                            if (chatGen == gen) handleSseBlock(sb.toString())
                            sb.setLength(0)
                        }
                    } else if (l.startsWith("data:")) {
                        if (sb.isNotEmpty()) sb.append('\n')
                        sb.append(l.removePrefix("data:").trimStart())
                    }
                }
                if (sb.isNotEmpty() && chatGen == gen) handleSseBlock(sb.toString())
            }
        } catch (e: Exception) {
            // 被新请求接管而断开的连接：readLine 抛异常属预期，静默退出，
            // 不向 WebView 推送"连接失败"（否则会污染新请求的气泡）
            if (chatGen != gen) {
                Log.d(TAG, "chat superseded, ignore error: $e")
            } else {
                Log.e(TAG, "chat error: $e")
                // e.message 可能含引号/换行，直接拼字符串会破坏 JSON 字面量，
                // 导致前端 JSON.parse 失败、气泡卡在"思考中"。用 JSONObject 转义。
                pushEvent(JSONObject()
                    .put("error", "连接失败: ${e.message}")
                    .put("done", true).toString())
            }
        } finally {
            // 仅当 chatConn 仍是本次连接时才清理（新请求已接管时不动新连接）
            if (chatConn != null && chatConn === conn) {
                try { chatConn?.disconnect() } catch (_: Exception) {}
                chatConn = null
            }
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
            // WebView 在 onDestroy 后已 destroy：evaluateJavascript 可能抛异常
            // 或打警告日志。此处吞掉，避免聊天线程因 UI 生命周期崩溃。
            try {
                webView?.evaluateJavascript(
                    "window.onChatEvent(${JSONObject.quote(dataJson)})", null)
            } catch (_: Exception) {}
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
