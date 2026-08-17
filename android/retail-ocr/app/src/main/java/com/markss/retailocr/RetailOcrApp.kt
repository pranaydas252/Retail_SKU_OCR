package com.markss.retailocr

import android.app.Application
import androidx.appcompat.app.AppCompatDelegate
import com.markss.retailocr.data.AppPreferences

class RetailOcrApp : Application() {

    override fun onCreate() {
        super.onCreate()
        AppPreferences.init(this)
        // Light theme only, by requirement. Pinned here rather than left to the
        // system so a device set to dark mode cannot restyle the confidence
        // bands, which carry safety meaning on the confirmation screen.
        AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_NO)
    }
}
