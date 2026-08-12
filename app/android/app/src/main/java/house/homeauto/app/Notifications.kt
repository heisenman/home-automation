package house.homeauto.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

/**
 * Notification plumbing.
 *
 * Severity maps to CHANNELS rather than to a priority int, because a channel is the only thing the user
 * can actually govern: they can let `critical` through Do Not Disturb and silence `info` entirely, per
 * severity, from system settings — without us shipping a settings screen for it.
 */
object Notifications {
    const val CHANNEL_CRITICAL = "alerts_critical"
    const val CHANNEL_WARNING = "alerts_warning"
    const val CHANNEL_INFO = "alerts_info"
    const val CHANNEL_SERVICE = "service_status"

    /** Fixed id for the foreground-service notification; alerts use their own derived ids. */
    const val ID_SERVICE = 1

    fun ensureChannels(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = context.getSystemService(NotificationManager::class.java) ?: return
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_CRITICAL, context.getString(R.string.channel_critical),
                NotificationManager.IMPORTANCE_HIGH).apply {
                description = context.getString(R.string.channel_critical_desc)
                // Let the household decide, but a tank-full or a dead sensor should be able to break through.
                setBypassDnd(true)
            })
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_WARNING, context.getString(R.string.channel_warning),
                NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = context.getString(R.string.channel_warning_desc)
            })
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_INFO, context.getString(R.string.channel_info),
                NotificationManager.IMPORTANCE_LOW).apply {
                description = context.getString(R.string.channel_info_desc)
            })
        // The "connected" notice is a legal requirement of a foreground service, not news. MIN keeps it
        // collapsed at the bottom of the shade with no sound.
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_SERVICE, context.getString(R.string.channel_service),
                NotificationManager.IMPORTANCE_MIN).apply {
                description = context.getString(R.string.channel_service_desc)
                setShowBadge(false)
            })
    }

    fun openAppIntent(context: Context): PendingIntent {
        val intent = Intent(context, MainActivity::class.java)
            .setFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        return PendingIntent.getActivity(
            context, 0, intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    /** The persistent foreground-service notification. [detail] carries connection state. */
    fun serviceNotification(context: Context, detail: String) =
        NotificationCompat.Builder(context, CHANNEL_SERVICE)
            .setContentTitle(context.getString(R.string.service_title))
            .setContentText(detail)
            .setSmallIcon(R.drawable.ic_stat_home)
            .setContentIntent(openAppIntent(context))
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setShowWhen(false)
            .build()

    /**
     * Raise one alert. The notification id is derived from the alert's stable key, so the same fault
     * re-firing updates its own notification rather than stacking a second copy of the same news.
     */
    fun postAlert(context: Context, alert: Alert) {
        val nm = NotificationManagerCompat.from(context)
        if (!nm.areNotificationsEnabled()) return
        val n = NotificationCompat.Builder(context, Alerts.channelFor(alert.severity))
            .setContentTitle(alert.title)
            .setContentText(alert.detail)
            .setStyle(NotificationCompat.BigTextStyle().bigText(alert.detail))
            .setSmallIcon(R.drawable.ic_stat_home)
            .setContentIntent(openAppIntent(context))
            .setAutoCancel(true)
            .setCategory(
                if (alert.severity == Alerts.SEV_CRITICAL) NotificationCompat.CATEGORY_ALARM
                else NotificationCompat.CATEGORY_STATUS
            )
            .build()
        try {
            nm.notify(alert.key, alert.key.hashCode(), n)
        } catch (_: SecurityException) {
            // POST_NOTIFICATIONS revoked between the check above and here. Nothing to do but stay alive.
        }
    }
}
