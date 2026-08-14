package com.oneagent

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {

    companion object {
        private const val CHANNEL = "com.oneagent/pet_overlay"
        private const val REQ_OVERLAY = 1001
    }

    private var pendingShow: Map<String, Any?>? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)

        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL)
            .setMethodCallHandler { call, result ->
                when (call.method) {
                    "isPermissionGranted" -> {
                        result.success(Settings.canDrawOverlays(this))
                    }
                    "requestPermission" -> {
                        pendingShow = null
                        val intent = Intent(
                            Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                            Uri.parse("package:$packageName"))
                        startActivityForResult(intent, REQ_OVERLAY)
                        result.success(null)
                    }
                    "show" -> {
                        val args = call.arguments as? Map<String, Any?> ?: emptyMap()
                        if (!Settings.canDrawOverlays(this)) {
                            pendingShow = args
                            val intent = Intent(
                                Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                                Uri.parse("package:$packageName"))
                            startActivityForResult(intent, REQ_OVERLAY)
                            result.success(false)
                            return@setMethodCallHandler
                        }
                        startPetOverlay(args)
                        result.success(true)
                    }
                    "hide" -> {
                        stopService(Intent(this, PetOverlayService::class.java))
                        result.success(true)
                    }
                    "isActive" -> {
                        result.success(PetOverlayService.instance != null)
                    }
                    "updateModel" -> {
                        val args = call.arguments as? Map<String, Any?> ?: emptyMap()
                        val intent = Intent(this, PetOverlayService::class.java)
                        // updateOnly：仅热切换模型，不重建悬浮窗；
                        // modelPath 为空时服务端回退到内置 Hiyori 模型
                        intent.putExtra("updateOnly", true)
                        intent.putExtra("modelPath", args["modelPath"] as? String)
                        intent.putExtra("modelFileName", args["modelFileName"] as? String)
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            startForegroundService(intent)
                        } else {
                            startService(intent)
                        }
                        result.success(true)
                    }
                    else -> result.notImplemented()
                }
            }
    }

    private fun startPetOverlay(args: Map<String, Any?>) {
        val intent = Intent(this, PetOverlayService::class.java)
        intent.putExtra("baseUrl", args["baseUrl"] as? String ?: "")
        intent.putExtra("apiKey", args["apiKey"] as? String ?: "")
        intent.putExtra("modelPath", args["modelPath"] as? String)
        intent.putExtra("modelFileName", args["modelFileName"] as? String)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent)
        } else {
            startService(intent)
        }
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_OVERLAY) {
            val granted = Settings.canDrawOverlays(this)
            if (granted) {
                pendingShow?.let { startPetOverlay(it) }
                pendingShow = null
            }
        }
    }
}
