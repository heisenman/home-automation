package house.homeauto.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Bring the alert lane back after a reboot. Without this the phone stops receiving house alerts the first
 * time it restarts overnight, and nothing tells you — the failure mode is silence, which is exactly the
 * failure mode an alerting system must not have.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        if (Prefs(context).alertsEnabled) AlertService.start(context)
    }
}
