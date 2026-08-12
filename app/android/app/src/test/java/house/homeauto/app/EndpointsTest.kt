package house.homeauto.app

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Endpoint normalisation. Pure JVM — runs under `gradle test`, no device or emulator.
 *
 * These are not decoration: the endpoint is typed by a human on a phone keyboard, and everything
 * downstream (the pinned origin, the app URL, the MQTT broker host) is derived from it.
 */
class EndpointsTest {

    @Test
    fun `bare host gets https, never http`() {
        // The pin only covers https. Silently accepting a bare host as http would drop us to an
        // unauthenticated origin without anyone noticing.
        assertEquals("https://192.168.0.210", Endpoints.normalize("192.168.0.210"))
    }

    @Test
    fun `port is preserved`() {
        assertEquals("https://192.168.0.200:8443", Endpoints.normalize("https://192.168.0.200:8443"))
    }

    @Test
    fun `paths queries and fragments are discarded`() {
        // This is an ORIGIN. Someone pasting the URL out of their browser lands here, and appUrl() owns
        // the path — keeping theirs would produce https://host/app//app/.
        assertEquals("https://192.168.0.210", Endpoints.normalize("https://192.168.0.210/app/?x=1#y"))
    }

    @Test
    fun `whitespace is tolerated`() {
        assertEquals("https://192.168.0.210", Endpoints.normalize("  192.168.0.210  "))
    }

    @Test
    fun `junk is rejected rather than guessed at`() {
        assertNull(Endpoints.normalize(null))
        assertNull(Endpoints.normalize(""))
        assertNull(Endpoints.normalize("   "))
        assertNull(Endpoints.normalize("ftp://192.168.0.210"))
        assertNull(Endpoints.normalize("http://"))
    }

    @Test
    fun `app url keeps the trailing slash`() {
        // /app redirects, /app/ is the shell. Getting this wrong costs a round trip on every launch.
        assertEquals("https://192.168.0.210/app/", Endpoints.appUrl("https://192.168.0.210"))
        assertEquals("https://192.168.0.210/app/", Endpoints.appUrl("https://192.168.0.210/"))
    }

    @Test
    fun `health url is the O(1) probe path`() {
        assertEquals("https://192.168.0.210/health", Endpoints.healthUrl("https://192.168.0.210"))
    }

    @Test
    fun `broker host follows the endpoint`() {
        assertEquals("192.168.0.210", Endpoints.brokerHost("https://192.168.0.210"))
        assertEquals("192.168.0.200", Endpoints.brokerHost("https://192.168.0.200:8443"))
    }

    @Test
    fun `same-host detection decides in-app vs browser`() {
        val base = "https://192.168.0.210"
        assertTrue(Endpoints.isSameHost(base, "https://192.168.0.210/app/devices"))
        assertFalse(Endpoints.isSameHost(base, "https://example.com/"))
        assertFalse(Endpoints.isSameHost(base, "not a url"))
    }

    @Test
    fun `defaults are the two hosts the certificate actually covers`() {
        // Both appear in the server cert SAN list (192.168.0.200, 192.168.0.210). ha-2's own
        // 192.168.1.200 is deliberately absent: unroutable from a phone and not in the SAN.
        assertEquals("192.168.0.210", Endpoints.brokerHost(Endpoints.DEFAULT_PRIMARY))
        assertEquals("192.168.0.200", Endpoints.brokerHost(Endpoints.DEFAULT_FALLBACK))
    }
}
