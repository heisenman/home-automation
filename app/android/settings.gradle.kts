// Android shell for the HA PWA (ADR-0038). Deliberately a SINGLE module: the app is a thin trusted
// shell (WebView + pinned trust + MQTT alert service), not a second UI codebase — see docs/app/ANDROID.md.
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "HomeAutomation"
include(":app")
