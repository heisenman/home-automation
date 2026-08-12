package house.homeauto.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Alert parsing, checked against the payloads the server actually publishes
 * (server/api/main.py publishes; server/api/viewmodel.py build_alerts authors the alert dicts).
 *
 * The fixtures below are the real wire shapes, not invented ones — a parser tested against made-up JSON
 * proves only that it parses made-up JSON.
 */
class AlertsTest {

    private val snapshot = """
        {"schema": 1, "ts": 1786514411.36, "alerts": [
          {"severity": "warning", "kind": "low_battery", "device_id": "meter_pro_h_bed",
           "name": "H Bedroom", "detail": "battery 14%"},
          {"severity": "critical", "kind": "tank_full", "device_id": "dehumidifier_living_room",
           "name": "Dehumidifier", "detail": "tank full — dehumidifier paused"}
        ]}
    """.trimIndent()

    private val event = """
        {"schema": 1, "ts": 1786514411.36, "alert":
          {"severity": "critical", "kind": "tank_full", "device_id": "dehumidifier_living_room",
           "name": "Dehumidifier", "detail": "tank full — dehumidifier paused"}}
    """.trimIndent()

    @Test
    fun `snapshot parses every alert`() {
        val alerts = Alerts.parseSnapshot(snapshot)
        assertEquals(2, alerts.size)
        assertEquals("low_battery", alerts[0].kind)
        assertEquals("battery 14%", alerts[0].detail)
        assertEquals("Dehumidifier", alerts[1].name)
    }

    @Test
    fun `empty snapshot is the healthy case, not an error`() {
        // This is what the house publishes most of the time; it must not look like a parse failure.
        assertEquals(0, Alerts.parseSnapshot("""{"schema": 1, "ts": 1.0, "alerts": []}""").size)
    }

    @Test
    fun `event parses the single alert`() {
        val a = Alerts.parseEvent(event)
        assertEquals("tank_full", a?.kind)
        assertEquals("critical", a?.severity)
    }

    @Test
    fun `alert key matches the server's alert_key`() {
        // Server: webpush.alert_key() == f"{kind}:{device_id}". Ours is the notification tag, so a
        // divergence here would stack duplicate notifications for one recurring fault.
        assertEquals("tank_full:dehumidifier_living_room", Alerts.parseEvent(event)?.key)
    }

    @Test
    fun `malformed payloads degrade to nothing rather than throwing`() {
        // A bad publish must not take down the alert service — it would take the alerts with it.
        assertEquals(0, Alerts.parseSnapshot("not json").size)
        assertEquals(0, Alerts.parseSnapshot(null).size)
        assertEquals(0, Alerts.parseSnapshot("""{"alerts": "wrong type"}""").size)
        assertNull(Alerts.parseEvent("not json"))
        assertNull(Alerts.parseEvent(null))
        assertNull(Alerts.parseEvent("""{"schema": 1}"""))
    }

    @Test
    fun `an alert without a kind is dropped`() {
        // kind is half the identity; without it every such alert would collide on the key ":device".
        assertEquals(0, Alerts.parseSnapshot("""{"alerts":[{"severity":"info","device_id":"x"}]}""").size)
    }

    @Test
    fun `missing severity falls back to the quietest`() {
        val a = Alerts.parseEvent("""{"alert":{"kind":"k","device_id":"d"}}""")
        assertEquals(Alerts.SEV_INFO, a?.severity)
        assertEquals(Notifications.CHANNEL_INFO, Alerts.channelFor(a?.severity))
    }

    @Test
    fun `severity ranking puts critical first`() {
        val sorted = Alerts.mostSevereFirst(Alerts.parseSnapshot(snapshot))
        assertEquals("critical", sorted.first().severity)
    }

    @Test
    fun `unknown severity is ranked last and routed to the quiet channel`() {
        assertTrue(Alerts.rank("bogus") > Alerts.rank(Alerts.SEV_INFO))
        assertEquals(Notifications.CHANNEL_INFO, Alerts.channelFor("bogus"))
    }

    @Test
    fun `each severity maps to its own channel`() {
        assertEquals(Notifications.CHANNEL_CRITICAL, Alerts.channelFor(Alerts.SEV_CRITICAL))
        assertEquals(Notifications.CHANNEL_WARNING, Alerts.channelFor(Alerts.SEV_WARNING))
        assertEquals(Notifications.CHANNEL_INFO, Alerts.channelFor(Alerts.SEV_INFO))
    }

    @Test
    fun `title falls back to device id when the alert has no name`() {
        val a = Alerts.parseEvent("""{"alert":{"kind":"k","device_id":"gas_kitchen"}}""")
        assertEquals("gas_kitchen", a?.title)
    }

    @Test
    fun `topics match the server's constants`() {
        assertEquals("home/_alerts", Alerts.TOPIC_SNAPSHOT)
        assertEquals("home/_alert/new", Alerts.TOPIC_EVENT)
    }
}
