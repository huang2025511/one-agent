package com.oneagent

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * 安全存储：API Key 等敏感值经 Android Keystore AES-256-GCM 加密后
 * 落入应用私有 SharedPreferences。密钥不可导出（硬件安全模块内生成），
 * 备份/拷贝 prefs 文件只能拿到密文。
 *
 * 之前 API Key 明文存 SharedPreferences（XML 直接可读），root/备份场景下泄露。
 */
class SecureStorage(context: Context) {

    private val prefs =
        context.getSharedPreferences("secure_storage", Context.MODE_PRIVATE)

    companion object {
        private const val KS_ALIAS = "oneagent_master_key"
        private const val GCM_IV_LEN = 12
        private const val GCM_TAG_BITS = 128
    }

    private fun getOrCreateSecretKey(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getEntry(KS_ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val gen = KeyGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(KeyGenParameterSpec.Builder(
            KS_ALIAS,
            KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .build())
        return gen.generateKey()
    }

    fun write(key: String, value: String) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateSecretKey())
        val blob = Base64.encodeToString(
            cipher.iv + cipher.doFinal(value.toByteArray(Charsets.UTF_8)),
            Base64.NO_WRAP)
        prefs.edit().putString(key, blob).apply()
    }

    fun read(key: String): String? {
        val blob = prefs.getString(key, null) ?: return null
        return try {
            val all = Base64.decode(blob, Base64.NO_WRAP)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(
                Cipher.DECRYPT_MODE, getOrCreateSecretKey(),
                GCMParameterSpec(GCM_TAG_BITS, all.copyOfRange(0, GCM_IV_LEN)))
            String(cipher.doFinal(all.copyOfRange(GCM_IV_LEN, all.size)), Charsets.UTF_8)
        } catch (_: Exception) {
            // 密钥轮换/数据损坏：视为不存在，调用方回退到空值
            null
        }
    }

    fun delete(key: String) {
        prefs.edit().remove(key).apply()
    }
}

class MainActivity : FlutterActivity() {

    companion object {
        private const val CHANNEL = "com.oneagent/pet_overlay"
        private const val SECURE_CHANNEL = "com.oneagent/secure_storage"
        private const val REQ_OVERLAY = 1001
    }

    private var pendingShow: Map<String, Any?>? = null
    private lateinit var secureStorage: SecureStorage

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        secureStorage = SecureStorage(this)

        // 敏感值安全存储通道（Android Keystore 加密）
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, SECURE_CHANNEL)
            .setMethodCallHandler { call, result ->
                val args = call.arguments as? Map<*, *>
                val key = args?.get("key") as? String
                if (key.isNullOrEmpty()) {
                    result.error("INVALID_ARGS", "key must be a non-empty string", null)
                    return@setMethodCallHandler
                }
                when (call.method) {
                    "read" -> result.success(secureStorage.read(key))
                    "write" -> {
                        val value = args["value"] as? String ?: ""
                        if (value.isEmpty()) {
                            secureStorage.delete(key)
                        } else {
                            secureStorage.write(key, value)
                        }
                        result.success(null)
                    }
                    "delete" -> {
                        secureStorage.delete(key)
                        result.success(null)
                    }
                    else -> result.notImplemented()
                }
            }

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
                        // 返回服务是否真的在运行：悬浮窗未启动时 stopService
                        // 是无害空操作，但把真实状态回传给 Dart 侧同步 _isActive，
                        // 避免本地状态与原生不一致（如用户已点 ✕ 关闭后再次 hide）
                        val wasActive = PetOverlayService.instance != null
                        if (wasActive) {
                            stopService(Intent(this, PetOverlayService::class.java))
                        }
                        result.success(wasActive)
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
