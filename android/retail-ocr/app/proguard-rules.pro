# kotlinx.serialization: keep generated serializers for the wire models.
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.**
-keepclassmembers class com.markss.retailocr.data.** {
    *** Companion;
}
-keepclasseswithmembers class com.markss.retailocr.data.** {
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class com.markss.retailocr.data.**$$serializer { *; }

# Retrofit / OkHttp
-dontwarn okhttp3.**
-dontwarn retrofit2.**
-keepattributes Signature, Exceptions

# Zebra EMDK is compileOnly; the runtime lives on the device.
-dontwarn com.symbol.emdk.**
