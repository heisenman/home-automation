"""Cluster heartbeat publisher (failover/README.md, ADR-0001/0011). Emits
`ha/cluster/<node>/heartbeat` every HEARTBEAT_S so the peer can sense this box's liveness/role —
redundant with VRRP adverts and the SSH/HTTP layers. Standalone systemd service; does NOT touch ha-api.

The `ha/cluster/#` namespace is separate from device `home/#` (no telemetry-loop risk). Heartbeats are
retained + backed by a retained last-will, so a peer connecting (or a node dying) sees current truth.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

import paho.mqtt.client as mqtt

from server.cluster.state import controller_active, node_id, read_cluster_env, vip_held

log = logging.getLogger("ha.cluster.heartbeat")

HEARTBEAT_S = float(os.environ.get("HA_HEARTBEAT_S", "3"))
BROKER = os.environ.get("HA_BROKER", "localhost")
PORT = int(os.environ.get("HA_BROKER_PORT", "1883"))
_PRIORITY = {"primary": 150, "standby": 100}


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    env = read_cluster_env()
    role = env.get("ROLE", "primary")
    node = node_id(role)
    vip = env.get("VIP", "192.168.0.200")
    topic = f"ha/cluster/{node}/heartbeat"

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"ha-cluster-{node}")
    try:                                          # broker creds if the broker is authenticated
        from server.util.mqtt_creds import apply_credentials
        apply_credentials(c)
    except Exception:
        pass
    c.will_set(topic, json.dumps({"node": node, "role": role, "controller_active": False,
                                  "healthy": False, "ts": 0, "lwt": True}), qos=1, retain=True)
    c.connect(BROKER, PORT, 60)
    c.loop_start()
    log.info("cluster heartbeat publishing %s every %.1fs (role=%s)", topic, HEARTBEAT_S, role)
    try:
        while True:
            active = controller_active()
            c.publish(topic, json.dumps({
                "node": node, "role": role, "priority": _PRIORITY.get(role, 100),
                "controller_active": active, "vip_held": vip_held(vip),
                "healthy": active, "ts": int(time.time()),
            }), qos=0, retain=True)
            time.sleep(HEARTBEAT_S)
    finally:
        c.loop_stop()
        c.disconnect()


if __name__ == "__main__":
    main()
