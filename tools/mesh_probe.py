#!/usr/bin/env python3
"""Populate the mesh-topology graph (mesh_links) from what the system already emits — no new radios.

Three observers, all passive:
  1. host→endpoint : the host's own hci1 hearing each endpoint, read from device_last_seen.last_rssi
                     (src = the server, link = ble-adv).
  2. node→endpoint : a short live sniff of  home/edge/<node>/<mac>/adv  — each advert carries the
                     relaying node and meta.rssi, exactly the per-node reach we currently discard.
  3. server→node   : seeing ANY message from a node proves its IP/MQTT backhaul is up (link = ip, ok).

node↔node (espnow) links are left for the multi-node future — when a node can report hearing another
node's beacon, add a 4th observer here; the graph + pathfinder already handle the extra hop.

Run periodically (a systemd timer) or ad-hoc. Idempotent: upserts links, accumulates ok/fail counters.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import yaml

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))
from server.mesh import store as S                      # noqa: E402
from server.mesh.topology import SERVER                 # noqa: E402

try:
    from server.util.mqtt_creds import apply_credentials
except Exception:                                       # pragma: no cover
    def apply_credentials(client):
        u = os.environ.get("HA_MQTT_USER")
        if u:
            client.username_pw_set(u, os.environ.get("HA_MQTT_PASS"))


def _mac_to_device(registry_path: Path) -> dict:
    reg = (yaml.safe_load(registry_path.read_text()) or {}).get("devices", {})
    return {mac.upper(): info.get("device_id") for mac, info in reg.items() if info.get("device_id")}


def seed_host_links(conn, db_path: Path) -> int:
    """host (server's own hci1) → endpoint, from device_last_seen."""
    n = 0
    try:
        rows = conn.execute("SELECT device_id, last_rssi FROM device_last_seen").fetchall()
    except sqlite3.Error:
        return 0
    for device_id, rssi in rows:
        S.record_link(conn, SERVER, ("endpoint", device_id), "ble-adv", rssi=rssi, ok=None)
        n += 1
    return n


def sniff_node_links(conn, mac2dev: dict, broker: str, port: int, seconds: int) -> dict:
    """Subscribe to edge adv for `seconds`, accumulating in memory (the paho callback runs in a
    different thread than `conn`, so we must not touch sqlite there), then persist in this thread.
    Records node→endpoint (ble-adv, strongest rssi seen) + server→node (ip, proven by any message)."""
    nodes_seen: set = set()
    best_rssi: dict = {}          # (node, device_id) -> strongest rssi observed

    def on_connect(c, u, f, rc, props=None):
        c.subscribe("home/edge/+/+/adv", qos=0)

    def on_message(c, u, msg):
        try:
            p = json.loads(msg.payload.decode())
        except Exception:
            return
        parts = msg.topic.split("/")
        node = p.get("node") or (parts[2] if len(parts) > 3 else None)
        if not node:
            return
        nodes_seen.add(node)
        dev = mac2dev.get(str(p.get("mac", "")).upper())
        if dev:
            rssi = (p.get("meta", {}) or {}).get("rssi")
            key = (node, dev)
            if rssi is not None and (key not in best_rssi or rssi > best_rssi[key]):
                best_rssi[key] = rssi

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(c)
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(broker, port, 30)
    c.loop_start()
    time.sleep(seconds)
    c.loop_stop()
    c.disconnect()

    # persist from the main thread
    for node in nodes_seen:
        S.record_link(conn, SERVER, ("node", node), "ip", ok=True)
    for (node, dev), rssi in best_rssi.items():
        S.record_link(conn, ("node", node), ("endpoint", dev), "ble-adv", rssi=rssi, ok=None)
    return {"node_endpoint": len(best_rssi), "server_node": nodes_seen}


def main() -> None:
    ap = argparse.ArgumentParser(description="Populate mesh_links from device_last_seen + a live adv sniff")
    ap.add_argument("--db", type=Path, default=_REPO / "instance" / "db" / "hot.db")
    ap.add_argument("--registry", type=Path, default=_REPO / "instance" / "devices.yaml")
    ap.add_argument("--broker", default=os.environ.get("HA_BROKER", "localhost"))
    ap.add_argument("--broker-port", type=int, default=int(os.environ.get("HA_BROKER_PORT", "1883")))
    ap.add_argument("--seconds", type=int, default=30, help="adv sniff duration")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    S.ensure_schema(conn)
    mac2dev = _mac_to_device(a.registry)
    host = seed_host_links(conn, a.db)
    sniff = sniff_node_links(conn, mac2dev, a.broker, a.broker_port, a.seconds)
    print(f"mesh_probe: host→endpoint links={host}; node→endpoint sightings={sniff['node_endpoint']}; "
          f"nodes seen={sorted(sniff['server_node'])}")
    total = conn.execute("SELECT count(*) FROM mesh_links").fetchone()[0]
    print(f"mesh_links now holds {total} edges")
    conn.close()


if __name__ == "__main__":
    main()
