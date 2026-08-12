package house.homeauto.app

import android.app.Service
import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Build
import android.os.IBinder
import android.provider.Settings
import android.util.Log
import androidx.core.app.NotificationManagerCompat
import com.hivemq.client.mqtt.MqttClient
import com.hivemq.client.mqtt.mqtt3.Mqtt3AsyncClient
import java.nio.charset.StandardCharsets
import java.util.concurrent.TimeUnit

/**
 * The LAN alert consumer.
 *
 * docs/decisions/air-gap-notify.md dropped vendor Web Push (it routes through Google/Mozilla, unreachable
 * once air-gapped) and moved alerts onto the system's own bus, closing with one open consequence: "no
 * background phone notification until a LAN consumer exists." This service is that consumer.
 *
 * It holds an MQTT subscription to the same two topics the wall panels read:
 *   home/_alerts     retained snapshot — seeds state on connect AND lets us clear notifications for
 *                    alerts that have since resolved
 *   home/_alert/new  edge-triggered, already de-duplicated server-side — each one is notify-worthy
 *
 * FOREGROUND TYPE IS `specialUse`, NOT `dataSync`, and that is deliberate: on Android 15 a dataSync
 * service is capped at ~6 cumulative hours per day and then stopped by the system, and it may not be
 * started from BOOT_COMPLETED. Either restriction alone would turn an always-on alert lane into one that
 * goes quiet without saying so — the exact failure mode an alerting system may not have.
 */
class AlertService : Service() {

    private var client: Mqtt3AsyncClient? = null
    private var connectivity: ConnectivityManager? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    /** Alert keys currently notified, so we can withdraw a notification once the fault clears. */
    private val notified = mutableSetOf<String>()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        Notifications.ensureChannels(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Must be immediate — the system gives a foreground service ~5s to post its notification.
        startForeground(Notifications.ID_SERVICE, Notifications.serviceNotification(this, getString(R.string.service_connecting)))

        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        connect()
        watchNetwork()
        return START_STICKY
    }

    private fun connect() {
        disconnectQuietly()
        val host = Endpoints.brokerHost(Prefs(this).endpoint)
        if (host == null) {
            updateStatus(getString(R.string.service_no_endpoint))
            return
        }

        @Suppress("HardwareIds") // Stable per-install id; only used so two phones get distinct client ids.
        val deviceId = Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID) ?: "unknown"

        val c = MqttClient.builder()
            .useMqttVersion3()
            .identifier("ha-android-${deviceId.take(8)}")
            .serverHost(host)
            .serverPort(Endpoints.BROKER_PORT)
            // Back off when the house is not reachable (phone off the home network) instead of spinning
            // the radio. The network callback below re-kicks us the moment WiFi returns, so a long
            // ceiling costs nothing in responsiveness.
            .automaticReconnect()
            .initialDelay(2, TimeUnit.SECONDS)
            .maxDelay(2, TimeUnit.MINUTES)
            .applyAutomaticReconnect()
            .addConnectedListener { onConnected() }
            .addDisconnectedListener { updateStatus(getString(R.string.service_disconnected)) }
            .buildAsync()
        client = c

        c.connectWith()
            .cleanSession(true)     // retained snapshot re-seeds us on every connect; no session to keep
            .keepAlive(60)
            .send()
            .whenComplete { _, err ->
                if (err != null) {
                    Log.w(TAG, "connect failed: ${err.message}")
                    updateStatus(getString(R.string.service_disconnected))
                }
            }
    }

    private fun onConnected() {
        updateStatus(getString(R.string.service_connected))
        val c = client ?: return
        c.subscribeWith().topicFilter(Alerts.TOPIC_SNAPSHOT).qos(com.hivemq.client.mqtt.datatypes.MqttQos.AT_MOST_ONCE)
            .callback { pub -> onSnapshot(payload(pub.payloadAsBytes)) }
            .send()
        c.subscribeWith().topicFilter(Alerts.TOPIC_EVENT).qos(com.hivemq.client.mqtt.datatypes.MqttQos.AT_MOST_ONCE)
            .callback { pub -> onEvent(payload(pub.payloadAsBytes)) }
            .send()
    }

    private fun payload(bytes: ByteArray?): String =
        bytes?.toString(StandardCharsets.UTF_8).orEmpty()

    /**
     * Retained full set. We do NOT notify from here — that would re-announce every standing alert on each
     * reconnect. Its job is withdrawal: anything we had notified that is no longer active gets cleared,
     * so the shade reflects the house rather than a history of it.
     */
    private fun onSnapshot(json: String) {
        val active = Alerts.parseSnapshot(json).map { it.key }.toSet()
        val nm = NotificationManagerCompat.from(this)
        synchronized(notified) {
            notified.filterNot { it in active }.forEach { key ->
                nm.cancel(key, key.hashCode())
                notified.remove(key)
            }
        }
        updateStatus(
            if (active.isEmpty()) getString(R.string.service_connected)
            else resources.getQuantityString(R.plurals.service_active_alerts, active.size, active.size)
        )
    }

    private fun onEvent(json: String) {
        val alert = Alerts.parseEvent(json) ?: return
        Notifications.postAlert(this, alert)
        synchronized(notified) { notified.add(alert.key) }
    }

    /** Reconnect promptly when the phone rejoins a network rather than waiting out the backoff. */
    private fun watchNetwork() {
        if (networkCallback != null) return
        val cm = getSystemService(ConnectivityManager::class.java) ?: return
        connectivity = cm
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                client?.let { if (!it.state.isConnectedOrReconnect) connect() } ?: connect()
            }
        }
        networkCallback = cb
        cm.registerNetworkCallback(
            NetworkRequest.Builder().addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET).build(), cb
        )
    }

    private fun updateStatus(text: String) {
        NotificationManagerCompat.from(this)
            .let { nm ->
                if (!nm.areNotificationsEnabled()) return
                try {
                    nm.notify(Notifications.ID_SERVICE, Notifications.serviceNotification(this, text))
                } catch (_: SecurityException) { /* permission pulled mid-flight */ }
            }
    }

    private fun disconnectQuietly() {
        try {
            client?.disconnect()
        } catch (_: Exception) { /* already gone */ }
        client = null
    }

    override fun onDestroy() {
        networkCallback?.let { cb -> runCatching { connectivity?.unregisterNetworkCallback(cb) } }
        networkCallback = null
        disconnectQuietly()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "AlertService"
        private const val ACTION_STOP = "house.homeauto.app.STOP"

        fun start(context: Context) {
            val i = Intent(context, AlertService::class.java)
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(i)
                } else {
                    context.startService(i)
                }
            } catch (e: Exception) {
                // Android forbids starting a foreground service from some background states. Not fatal:
                // opening the app starts it, and BootReceiver retries on the next boot.
                Log.w(TAG, "could not start alert service: ${e.message}")
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, AlertService::class.java))
        }

        /** The lane is bound to the endpoint, so an endpoint change has to re-dial. */
        fun restart(context: Context) {
            stop(context)
            start(context)
        }
    }
}
