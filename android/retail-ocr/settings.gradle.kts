pluginManagement {
    repositories {
        google {
            content {
                includeGroupByRegex("com\\.android.*")
                includeGroupByRegex("com\\.google.*")
                includeGroupByRegex("androidx.*")
            }
        }
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()

        // Zebra's Maven repository, for the EMDK and Link-OS SDKs.
        //
        // Declared but not yet depended on. The EMDK artifact is compileOnly
        // and the ZSDK AAR will be vendored into app/libs, so the build does
        // not currently reach out to Zebra. See CLAUDE.md sections 4 and 18.
        maven {
            url = uri("https://zebratech.jfrog.io/artifactory/EMDK-Android/")
            content { includeGroup("com.symbol") }
        }
    }
}

rootProject.name = "RetailOcr"
include(":app")
