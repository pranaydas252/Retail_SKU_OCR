package com.markss.dmartocr

import android.app.Application
import androidx.appcompat.app.AppCompatDelegate

class DmartOcrApp : Application() {

    override fun onCreate() {
        super.onCreate()
        // Light theme only, by requirement. Pinned here rather than left to the
        // system so a device set to dark mode cannot restyle the confidence
        // bands, which carry safety meaning on the confirmation screen.
        AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_NO)
    }
}
