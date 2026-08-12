package house.homeauto.app

import android.content.Context

/**
 * App settings. Deliberately small: the endpoint and whether the alert lane should run.
 *
 * The admin bearer token is NOT here — it stays in the WebView's own DOM storage where the PWA already
 * puts it (server/web/app.js derives SHA-256("ha-api:"+master) and stores only that hash, never the
 * passphrase). Inside an app, that storage is private to this UID rather than shared with every other tab
 * in a browser profile, so wrapping it in a second store would add moving parts without adding protection.
 */
class Prefs(context: Context) {
    private val sp = context.applicationContext.getSharedPreferences("ha.shell", Context.MODE_PRIVATE)

    /** Configured origin, or the built-in primary if the user has never chosen one. */
    var endpoint: String
        get() = sp.getString(KEY_ENDPOINT, null) ?: Endpoints.DEFAULT_PRIMARY
        set(value) = sp.edit().putString(KEY_ENDPOINT, value).apply()

    /** Whether the MQTT alert service should be running. Default on — background alerts are the point. */
    var alertsEnabled: Boolean
        get() = sp.getBoolean(KEY_ALERTS, true)
        set(value) = sp.edit().putBoolean(KEY_ALERTS, value).apply()

    private companion object {
        const val KEY_ENDPOINT = "endpoint"
        const val KEY_ALERTS = "alerts_enabled"
    }
}
