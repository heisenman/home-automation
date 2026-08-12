package house.homeauto.app

import org.json.JSONObject

/**
 * One active alert, mirroring the server's build_alerts() dict exactly
 * (server/api/viewmodel.py: {severity, kind, device_id, name, detail}).
 */
data class Alert(
    val severity: String,
    val kind: String,
    val deviceId: String,
    val name: String,
    val detail: String,
) {
    /**
     * Stable identity — must match server-side webpush.alert_key() ("kind:device_id"). Used as the
     * notification tag so a re-fired alert REPLACES its predecessor instead of stacking a duplicate.
     */
    val key: String get() = "$kind:$deviceId"

    /** What the user reads on the lock screen. */
    val title: String get() = if (name.isNotBlank()) name else deviceId
}

/**
 * Parsers for the air-gap-native alert bus (docs/decisions/air-gap-notify.md).
 *
 * Two topics, two shapes, two jobs:
 *   home/_alerts    RETAINED  {schema, ts, alerts:[...]}  — full current set, delivered on connect.
 *                             Seeds state so a cold start knows what is wrong without waiting for a change.
 *   home/_alert/new NOT retained {schema, ts, alert:{...}} — one publish per NEWLY-appeared alert.
 *                             The server already does the edge detection, so each of these is notify-worthy.
 *
 * Pure — unit-tested in AlertsTest against the real published payloads.
 */
object Alerts {
    const val TOPIC_SNAPSHOT = "home/_alerts"
    const val TOPIC_EVENT = "home/_alert/new"

    const val SEV_CRITICAL = "critical"
    const val SEV_WARNING = "warning"
    const val SEV_INFO = "info"

    /** Parse the retained snapshot. Malformed input yields an empty list, never an exception —
     *  a bad payload must not take down the alert service. */
    fun parseSnapshot(json: String?): List<Alert> {
        val root = try {
            JSONObject(json ?: return emptyList())
        } catch (_: Exception) {
            return emptyList()
        }
        val arr = root.optJSONArray("alerts") ?: return emptyList()
        val out = ArrayList<Alert>(arr.length())
        for (i in 0 until arr.length()) {
            arr.optJSONObject(i)?.let { fromJson(it) }?.let(out::add)
        }
        return out
    }

    /** Parse a single edge-triggered event. Null if the payload is malformed or carries no alert. */
    fun parseEvent(json: String?): Alert? {
        val root = try {
            JSONObject(json ?: return null)
        } catch (_: Exception) {
            return null
        }
        return root.optJSONObject("alert")?.let { fromJson(it) }
    }

    private fun fromJson(o: JSONObject): Alert? {
        val kind = o.optString("kind").ifBlank { return null }
        return Alert(
            severity = o.optString("severity").ifBlank { SEV_INFO },
            kind = kind,
            deviceId = o.optString("device_id"),
            name = o.optString("name"),
            detail = o.optString("detail"),
        )
    }

    /** Severity ordering, most severe first — same ranking the PWA service worker used. */
    fun rank(severity: String?): Int = when (severity) {
        SEV_CRITICAL -> 0
        SEV_WARNING -> 1
        SEV_INFO -> 2
        else -> 3
    }

    fun mostSevereFirst(alerts: List<Alert>): List<Alert> = alerts.sortedBy { rank(it.severity) }

    /** Notification channel id for a severity. Unknown severities land in the quietest channel. */
    fun channelFor(severity: String?): String = when (severity) {
        SEV_CRITICAL -> Notifications.CHANNEL_CRITICAL
        SEV_WARNING -> Notifications.CHANNEL_WARNING
        else -> Notifications.CHANNEL_INFO
    }
}
