"""Cluster/supervisor alerts for the PWA banner (plan crystalline-spinning-spindle, Layer 5).

`build_alerts` derives device alerts from sensors/displays; it can't see SERVICE/CLUSTER health. This keeps a
tiny cache fed from the RETAINED cluster topics (reconnect-safe — a fresh API instance gets current truth):
  - home/_supervisor/<host>/status  {host, gaps, units, ts}   (required_services + service_healer)
  - home/_cluster/<node>/liveness    {node, up, ts}           (node_watch)
`to_alerts(now)` normalizes the unhealthy ones into the same shape build_alerts emits, so the existing PWA
AlertsBanner renders them with no client change. Read-only; best-effort (empty if the broker is unreachable).
"""
from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("ha.cluster_alerts")

SUPERVISOR_TOPIC = "home/_supervisor/+/status"
LIVENESS_TOPIC = "home/_cluster/+/liveness"
STALE_S = 1800.0          # drop a retained beacon whose host/node went silent this long (avoid stuck banner)


class ClusterAlertCache:
    def __init__(self) -> None:
        self._supervisor: dict[str, dict] = {}     # host -> {gaps, units, ts}
        self._liveness: dict[str, dict] = {}       # node -> {up, ts}

    def ingest(self, topic: str, payload: dict) -> None:
        parts = topic.split("/")
        if topic.startswith("home/_supervisor/") and len(parts) == 4:
            self._supervisor[parts[2]] = payload
        elif topic.startswith("home/_cluster/") and len(parts) == 4 and parts[3] == "liveness":
            self._liveness[payload.get("node") or parts[2]] = payload

    def forget(self, topic: str) -> None:
        """A retained-clear (empty payload) removes the entry entirely, so a deleted beacon can't linger."""
        parts = topic.split("/")
        if topic.startswith("home/_supervisor/") and len(parts) == 4:
            self._supervisor.pop(parts[2], None)
        elif topic.startswith("home/_cluster/") and len(parts) == 4:
            self._liveness.pop(parts[2], None)

    @staticmethod
    def _fresh(ts, now: float) -> bool:
        # ts may be an epoch (supervisor beacon) or an ISO string (node_watch liveness) or absent — only apply
        # the staleness cutoff when it's a real number; otherwise treat as fresh rather than crash/drop.
        return not isinstance(ts, (int, float)) or (now - ts <= STALE_S)

    def to_alerts(self, now: float) -> list[dict]:
        out: list[dict] = []
        for host, s in self._supervisor.items():
            if s.get("gaps", 0) and self._fresh(s.get("ts"), now):
                units = ", ".join(s.get("units") or []) or f"{s['gaps']} unit(s)"
                out.append({"severity": "critical", "kind": "service_missing",
                            "device_id": f"host:{host}", "name": host,
                            "detail": f"service(s) down: {units}"})
        for node, l in self._liveness.items():
            if l.get("up") is False and self._fresh(l.get("ts"), now):
                out.append({"severity": "critical", "kind": "node_down",
                            "device_id": f"node:{node}", "name": f"node {node}",
                            "detail": "cluster heartbeat stale"})
        return out


def start_cluster_alert_subscriber(cache: ClusterAlertCache, *, broker: str = "localhost", port: int = 1883):
    """Best-effort paho subscriber feeding `cache` from the retained cluster topics. Returns the client
    (loop_stop/disconnect on shutdown) or None — in which case the banner just omits cluster alerts."""
    try:
        import paho.mqtt.client as mqtt
        from server.util.mqtt_creds import apply_credentials
    except Exception:
        log.warning("cluster_alerts: paho unavailable — banner omits cluster/service alerts")
        return None

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(client)

    def on_connect(c, u, flags, rc, properties=None):
        if rc == 0:
            c.subscribe([(SUPERVISOR_TOPIC, 0), (LIVENESS_TOPIC, 0)])
            log.info("cluster_alerts subscribed to %s + %s", SUPERVISOR_TOPIC, LIVENESS_TOPIC)

    def on_message(c, u, msg):
        if not msg.payload:                      # retained-clear (empty payload) -> forget the entry
            cache.forget(msg.topic)
            return
        try:
            cache.ingest(msg.topic, json.loads(msg.payload.decode()))
        except Exception:
            log.debug("cluster_alerts: undecodable payload on %s", msg.topic, exc_info=True)

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(broker, port, keepalive=60)
    except Exception:
        log.warning("cluster_alerts: broker %s:%s unreachable — cluster alerts disabled this cycle",
                    broker, port, exc_info=True)
        return None
    client.loop_start()
    return client
