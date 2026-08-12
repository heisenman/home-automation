package house.homeauto.app

import java.net.URI

/**
 * Where the house lives, and how to talk to it.
 *
 * A browser has an origin; an app does not — so the endpoint is a first-class setting rather than
 * something the user retypes. Both defaults below are in the server cert's SAN list, which is what makes
 * the pin in network_security_config.xml work for them:
 *
 *   - PRIMARY  192.168.0.210      the nginx bridge (household side only) that proxies to the air-gapped
 *                                 dictator ha-2. This is the production path.
 *   - FALLBACK 192.168.0.200:8443 the VIP, i.e. whichever node currently holds it.
 *
 * 192.168.1.200 (ha-2 direct) is deliberately NOT offered: it lives on the air-gap leg, a phone cannot
 * route to it, and it is not in the cert SAN list so the pin would reject it anyway.
 *
 * Pure functions — unit-tested in EndpointsTest, no Android framework needed.
 */
object Endpoints {
    const val DEFAULT_PRIMARY = "https://192.168.0.210"
    const val DEFAULT_FALLBACK = "https://192.168.0.200:8443"

    /** Plain MQTT. The broker runs on the same host as the web bridge (verified: household listeners on
     *  192.168.0.210:1883 and 192.168.0.200:1883), so the alert lane follows the endpoint automatically. */
    const val BROKER_PORT = 1883

    /**
     * Coerce whatever the user typed into a bare scheme://host[:port] origin, or null if it cannot be one.
     * Bare hosts get https:// (never http — the pin is the whole point). Paths, queries and fragments are
     * discarded: this is an origin, and appUrl/healthUrl own the paths.
     */
    fun normalize(raw: String?): String? {
        val trimmed = raw?.trim().orEmpty()
        if (trimmed.isEmpty()) return null
        val withScheme = if (trimmed.contains("://")) trimmed else "https://$trimmed"
        val uri = try {
            URI(withScheme)
        } catch (_: Exception) {
            return null
        }
        val scheme = uri.scheme?.lowercase() ?: return null
        if (scheme != "https" && scheme != "http") return null
        val host = uri.host ?: return null
        if (host.isBlank()) return null
        return if (uri.port > 0) "$scheme://$host:${uri.port}" else "$scheme://$host"
    }

    /** The PWA itself. Trailing slash matters — /app is a redirect, /app/ is the shell. */
    fun appUrl(base: String): String = "${base.trimEnd('/')}/app/"

    /**
     * Reachability probe. /health is deliberately O(1) (status + row count + newest ts) — the same
     * property keepalived's probe needed, and the reason it is safe to call on every resume.
     */
    fun healthUrl(base: String): String = "${base.trimEnd('/')}/health"

    /** Broker host for the alert lane — same box as the endpoint. Null if the base is unparseable. */
    fun brokerHost(base: String): String? = try {
        URI(base).host
    } catch (_: Exception) {
        null
    }

    /** True when [url] belongs to [base]'s host, i.e. should stay inside the app rather than open a browser. */
    fun isSameHost(base: String, url: String): Boolean {
        val a = brokerHost(base) ?: return false
        val b = try {
            URI(url).host
        } catch (_: Exception) {
            null
        }
        return a.equals(b, ignoreCase = true)
    }
}
