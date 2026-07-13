# Flutter 相关类不混淆
-keep class io.flutter.** { *; }
-keep class io.flutter.plugins.** { *; }

# freezed 生成的类不混淆（数据模型）
-keep class **_freezed.* { *; }
-keep class **.g.* { *; }
-keep class **.freezed.* { *; }
-keepattributes Signature
-keepattributes *Annotation*

# Riverpod 相关
-keep class com.riverpod.** { *; }

# 保留泛型签名（JSON 序列化需要）
-keepattributes InnerClasses
-keep class **_Internal* { *; }
