"""
Cluster node-failure watcher — the "notice node failures" layer (complements VRRP).

VRRP/keepalived MOVES the VIP on failover but never TELLS anyone a node died, and can't see "box up but
stack dead" (it keeps advertising while the app is down). The cluster heartbeat (server/cluster/heartbeat.py)
publishes `ha/cluster/<node>/heartbeat` every few seconds — a bus-observable, app-level liveness signal. This
watcher subscribes to every peer's heartbeat and raises an ALERT when one goes stale (node/stack down) and
again when it recovers. Runs on each node; a node watches its PEERS (it can't alert its own death — a live
peer does that).

Alert -> `home/_alert/new` (kind: node_down / node_up). Also emits retained `home/_cluster/<node>/liveness`
so a dashboard / cluster-doctor can read current truth.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

import paho.mqtt.client as mqtt

from server.util.mqtt_creds import apply_credentials

# Our own cluster label (never watch ourselves). HA_NODE_ID wins (lets a standby's watcher run against the
# dictator's broker with the right self-id); else derive from this box's role in cluster.env. node_id(role) is
# the project's role->label map ("210"/"245") — distinct within a pair, which is all the watcher needs.
SELF = os.environ.get("HA_NODE_ID", "")
if not SELF:
    try:
        from server.cluster.state import node_id, read_cluster_env
        SELF = node_id(read_cluster_env().get("ROLE", "primary"))
    except Exception:
        SELF = ""

log = logging.getLogger("ha.cluster.node_watch")

BROKER = os.environ.get("HA_BROKER", "localhost")
PORT = int(os.environ.get("HA_BROKER_PORT", "1883"))
# heartbeat cadence is ~3s; stale after this many seconds with no fresh ts = node down.
STALE_S = float(os.environ.get("HA_NODE_STALE_S", "15"))
CHECK_S = 5.0


class NodeWatch:
    def __init__(self, client: mqtt.Client):
        self._c = client
        self._last: dict[str, float] = {}     # node -> wall time of last FRESH heartbeat we saw
        self._down: set[str] = set()          # nodes currently in the alerted-down state
        self._degraded: set[str] = set()      # nodes alive but reporting services_ok=false (plan Stage B)

    def on_connect(self, c, u, f, rc, props=None):
        c.subscribe("ha/cluster/+/heartbeat", qos=1)
        log.info("watching ha/cluster/+/heartbeat (self=%s excluded, stale>%.0fs)", SELF, STALE_S)

    def on_message(self, c, u, msg):
        parts = msg.topic.split("/")          # ha/cluster/<node>/heartbeat
        if len(parts) != 4:
            return
        node = parts[2]
        if node == SELF:                      # never watch ourselves
            return
        try:
            d = json.loads(msg.payload.decode())
        except (ValueError, UnicodeDecodeError):
            d = {}
        ts = d.get("ts")
        # a heartbeat with a fresh ts = the peer's stack is alive right now
        if isinstance(ts, (int, float)) and (time.time() - ts) < STALE_S:
            self._last[node] = time.time()
            if node in self._down:            # it's back
                self._down.discard(node)
                self._alert(node, "node_up", "info", d, "recovered")
                self._liveness(node, True)
            # Stage B: peer is ALIVE but its healer reports an escalated service gap (services_ok=false). VRRP
            # can't see this; surface it as node_degraded (edge-triggered, like down/up). The peer's OWN healer
            # is self-healing/peer-repairing; this is the "failover monitors services on the dictator" signal.
            svc_ok = d.get("services_ok")
            if svc_ok is False and node not in self._degraded:
                self._degraded.add(node)
                self._alert(node, "node_degraded", "warning", d,
                            "alive but a required service is unhealed (services_ok=false)")
            elif svc_ok is True and node in self._degraded:
                self._degraded.discard(node)
                self._alert(node, "node_undegraded", "info", d, "services recovered")
        # a payload without a fresh ts (e.g. a retained last-will 'offline') is handled by the staleness sweep

    def sweep(self):
        now = time.time()
        for node, seen in list(self._last.items()):
            age = now - seen
            if age > STALE_S and node not in self._down:
                self._down.add(node)
                self._alert(node, "node_down", "critical", {"last_seen_age_s": round(age)},
                            f"no heartbeat for {age:.0f}s — node/stack DOWN (VRRP won't tell you this)")
                self._liveness(node, False)

    def _alert(self, node, kind, sev, extra, why):
        alert = {"id": f"{kind}:{node}", "kind": kind, "severity": sev, "node": node,
                 "ts": _iso(), "message": f"cluster node '{node}': {why}", **extra}
        try:
            self._c.publish("home/_alert/new", json.dumps(alert), qos=1)
            log.warning("%s: %s", kind.upper(), alert["message"])
        except Exception as exc:              # never die on a publish
            log.error("alert publish failed: %s", exc)

    def _liveness(self, node, up):
        try:
            self._c.publish(f"home/_cluster/{node}/liveness",
                            json.dumps({"node": node, "up": up, "ts": _iso()}), qos=1, retain=True)
        except Exception:
            pass


def _iso() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(c)
    w = NodeWatch(c)
    c.on_connect = w.on_connect
    c.on_message = w.on_message
    c.connect(BROKER, PORT, keepalive=30)
    c.loop_start()
    try:
        while True:
            time.sleep(CHECK_S)
            w.sweep()
    finally:
        c.loop_stop()
        c.disconnect()


if __name__ == "__main__":
    main()
