"""MQTT → ntfy bridge — air-gap-native phone notifications (decided 2026-06-25, replaces vendor Web Push).

Subscribes to the alert event bus (`home/_alert/new`, published by the API alert loop on the dictator) and
POSTs each new alert to a SELF-HOSTED ntfy server on the LAN. Phones run the ntfy app pointed at that local
server — push with no vendor cloud, so it survives the air gap. VIP-gated: only the box holding the VIP
notifies (matches the alert source; no double-notify across a failover).

  env: HA_BROKER/HA_BROKER_PORT (broker), NTFY_URL (e.g. http://localhost:8095), NTFY_TOPIC (ha-alerts),
       NTFY_TOKEN (optional bearer), HA_VIP (gate; unset = single box, always notify).

The alert→ntfy mapping is a pure function (unit-tested). See docs/decisions/air-gap-notify.md.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger("ha.ntfy")

# alert severity (build_alerts) -> ntfy priority 1..5 (5 = max/urgent, 3 = default)
_SEVERITY_PRIORITY = {"critical": 5, "warning": 4, "info": 2}
# alert kind -> ntfy tag(s); names that match emoji shortcodes render as emoji in the app
_KIND_TAGS = {
    "low_battery": ["battery"],
    "unreachable": ["warning"],
    "tank_full": ["droplet"],
    "override_expiring": ["hourglass"],
}
ALERT_EVENT_TOPIC = "home/_alert/new"


def alert_to_ntfy(alert: dict, topic: str) -> dict:
    """Map one alert {severity,kind,device_id,name,detail} to an ntfy publish payload. Pure."""
    severity = alert.get("severity", "info")
    name = alert.get("name") or alert.get("device_id") or "device"
    kind = str(alert.get("kind", "alert"))
    detail = alert.get("detail") or ""
    title = f"{name}: {kind.replace('_', ' ')}"
    return {
        "topic": topic,
        "title": title,
        "message": detail or title,
        "priority": _SEVERITY_PRIORITY.get(severity, 3),
        "tags": _KIND_TAGS.get(kind, ["bell"]),
    }


def post_ntfy(base_url: str, payload: dict, token: str | None = None, timeout: float = 10) -> int:
    """POST a publish payload to ntfy (JSON publishing: POST base_url/ with {topic,...}). Returns HTTP status."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(base_url.rstrip("/") + "/", data=data,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 (trusted LAN URL from env)
        return r.status


def main() -> None:
    import paho.mqtt.client as mqtt

    from server.util.mqtt_creds import apply_credentials

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    broker = os.environ.get("HA_BROKER", "localhost")
    port = int(os.environ.get("HA_BROKER_PORT", "1883"))
    ntfy_url = os.environ.get("NTFY_URL", "http://localhost:8095")
    topic = os.environ.get("NTFY_TOPIC", "ha-alerts")
    token = os.environ.get("NTFY_TOKEN") or None
    vip = os.environ.get("HA_VIP", "")

    def _holds_vip() -> bool:
        if not vip:
            return True                                   # single box / no failover -> always notify
        try:
            from server.cluster.state import vip_held
            return vip_held(vip)
        except Exception:
            return True                                   # fail-open: better a notification than silence

    def on_message(client, userdata, msg):
        if not _holds_vip():
            return                                        # standby never notifies (one-notifier invariant)
        try:
            alert = (json.loads(msg.payload.decode()) or {}).get("alert") or {}
        except Exception:
            return
        if not alert:
            return
        try:
            status = post_ntfy(ntfy_url, alert_to_ntfy(alert, topic), token)
            log.info("ntfy <- %s/%s (HTTP %s)", alert.get("kind"), alert.get("device_id"), status)
        except Exception:
            log.exception("ntfy POST failed for %s (continuing)", alert.get("kind"))

    def on_connect(cl, u, f, rc, props=None):
        cl.subscribe(ALERT_EVENT_TOPIC, qos=1)
        log.info("ntfy-bridge up: %s -> %s/%s (vip-gated=%s)", ALERT_EVENT_TOPIC, ntfy_url, topic, bool(vip))

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(c)
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(broker, port, 60)
    c.loop_forever()


if __name__ == "__main__":
    main()
