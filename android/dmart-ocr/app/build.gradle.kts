import java.util.Properties

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.serialization)
}

// Backend URL and API key come from local.properties (git-ignored) so no
// endpoint or credential is ever hard-coded into source (CLAUDE.md §19).
val localProps = Properties().apply {
    val file = rootProject.file("local.properties")
    if (file.exists()) file.inputStream().use { load(it) }
}

fun localOr(key: String, fallback: String): String =
    (localProps.getProperty(key) ?: System.getenv(key) ?: fallback)

android {
    namespace = "com.markss.dmartocr"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.markss.dmartocr"
        // TC22 ships Android 13. 26 keeps the project buildable and testable
        // on ordinary hardware during development; the Zebra restriction is
        // enforced at runtime, not by minSdk.
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        buildConfigField(
            "String",
            "BACKEND_BASE_URL",
            "\"${localOr("dmart.backendUrl", "http://10.0.2.2:8000/")}\""
        )
        buildConfigField(
            "String",
            "API_KEY",
            "\"${localOr("dmart.apiKey", "")}\""
        )
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
            // Development bypass for the Zebra-only gate. Without this the app
            // is untestable on any machine until a TC22 is physically present,
            // which would stall the whole phase (PLAN.md R10).
            buildConfigField("boolean", "ENFORCE_ZEBRA_ONLY", "false")
            // Cleartext is allowed in debug only, so a laptop-hosted backend
            // can be used before HTTPS is configured.
            manifestPlaceholders["usesCleartextTraffic"] = "true"
        }
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            buildConfigField("boolean", "ENFORCE_ZEBRA_ONLY", "true")
            manifestPlaceholders["usesCleartextTraffic"] = "false"
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
        buildConfig = true
    }

    testOptions {
        unitTests.isIncludeAndroidResources = true
    }
}

dependencies {
    // Zebra Link-OS Multiplatform SDK (ZSDK), for ZQ320 printing over Bluetooth
    // (CLAUDE.md §18). Not on Maven Central — it ships as a manual download
    // from Zebra behind an EULA, so the jar is vendored here and git-ignored.
    // See android/dmart-ocr/README.md for how to obtain it.
    implementation(fileTree(mapOf("dir" to "libs", "include" to listOf("*.jar"))))

    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.appcompat)
    implementation(libs.material)
    implementation(libs.androidx.constraintlayout)
    implementation("androidx.gridlayout:gridlayout:1.0.0")
    implementation(libs.androidx.activity.ktx)
    implementation(libs.androidx.lifecycle.viewmodel.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)

    implementation(libs.camera.core)
    implementation(libs.camera.camera2)
    implementation(libs.camera.lifecycle)
    implementation(libs.camera.view)

    implementation(libs.retrofit)
    implementation(libs.retrofit.serialization)
    implementation(libs.okhttp)
    implementation(libs.okhttp.logging)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.android)

    // Zebra EMDK, when the SDK becomes available (PLAN.md Q4 / R6).
    //
    // compileOnly by design: the EMDK runtime ships on the device, so the APK
    // still builds and installs on non-Zebra hardware and the restriction is a
    // runtime check. ZebraGate currently uses PackageManager and Build checks
    // only, which detect the same EMDK runtime without the artifact, so the
    // build does not depend on Zebra repository access.
    //
    // compileOnly("com.symbol:emdk:+")

    testImplementation(libs.junit)
    testImplementation(libs.robolectric)
    androidTestImplementation(libs.androidx.junit)
    androidTestImplementation(libs.espresso.core)
}
