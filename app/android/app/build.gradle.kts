import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Release signing material lives in instance/ (gitignored, indexed in docs/SECRETS.md) — never in git.
// THE KEY IS LOAD-BEARING FOREVER: Android refuses to update an installed app whose signature changed,
// so losing it means every phone must uninstall (losing its settings) before it can take an update.
val releaseKeystore = rootProject.file("../../instance/android-release.keystore")
val releaseKeyProps = rootProject.file("../../instance/android-release.properties")

android {
    namespace = "house.homeauto.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "house.homeauto.app"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }

    signingConfigs {
        if (releaseKeystore.exists() && releaseKeyProps.exists()) {
            create("release") {
                val props = Properties().apply { releaseKeyProps.inputStream().use { load(it) } }
                storeFile = releaseKeystore
                storePassword = props.getProperty("storePassword")
                keyAlias = props.getProperty("keyAlias")
                keyPassword = props.getProperty("keyPassword")
                // v3 is what makes the "key is load-bearing forever" note survivable: it is the scheme
                // that carries proof-of-rotation, so a compromised or lost-then-recovered key can be
                // rotated without every phone having to uninstall first. v2 stays on for the floor.
                enableV2Signing = true
                enableV3Signing = true
            }
        }
    }

    buildTypes {
        release {
            // No shrinking: the app is ~a dozen classes. R8 would buy nothing and only risk stripping
            // something the WebView/MQTT reflection paths need.
            isMinifyEnabled = false
            // Fall back to unsigned rather than to the DEBUG key: a debug-signed APK is debuggable, which
            // would let anyone with USB access attach to a process holding the admin bearer.
            signingConfig = signingConfigs.findByName("release")
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
    }

    // The MQTT client pulls in Netty, whose six modules each ship their own copy of these JAR metadata
    // files. The APK format has one flat resource namespace, so duplicates are a hard error rather than a
    // last-one-wins. None of them are read at runtime — they are build/provenance metadata.
    packaging {
        resources {
            excludes += setOf(
                "META-INF/INDEX.LIST",
                "META-INF/DEPENDENCIES",
                "META-INF/io.netty.versions.properties",
                "META-INF/LICENSE*",
                "META-INF/NOTICE*",
            )
        }
    }
    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")

    // Air-gap-native alert transport (docs/decisions/air-gap-notify.md). HiveMQ, not Paho: the Paho
    // Android service is unmaintained/deprecated.
    implementation("com.hivemq:hivemq-mqtt-client:1.3.3")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
