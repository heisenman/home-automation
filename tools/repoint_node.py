#!/usr/bin/env python3
"""
Repoint an ESP32 edge node onto a new Wi-Fi + broker with the signed `repoint` command (DJ-19).

This is the ESP-IDF class repointer for the air-gap migration — the counterpart to
`repoint_tasmota.py` (Tasmota Backlog) for the native-C fleet. It publishes ONE signed command on the
node's CURRENT broker:

    {"op":"repoint","ssid":..,"psk":..,"broker":"mqtt://<ha-2>:1883","ntp":..,"ota_host":..}

The firmware writes those into its "ha" NVS overlay, arms a boot-count revert, and reboots onto the new
network. Because the node comes back on the NEW broker (not the one we published from), this tool does
NOT confirm arrival — that is `device_push.py`'s job via the bridge (ha-2's API shows a fresh ts). Here
we optionally watch the OLD broker for the node's LWT/offline as evidence it accepted the command and left.

PRECONDITION: the node must already be running repoint-capable firmware (the `repoint` op). Older images
log "unknown cmd" and ignore this — harmless, but nothing moves. Sequence OTA-then-repoint via device_push.

  HA_CMD_SECRET=<node-secret> python3 tools/repoint_node.py --node gas_standby \
      --ssid autohome_airgap --psk "$WIFI_PSK" --broker mqtt://192.168.1.210:1883 \
      --ntp 192.168.1.210 --ota-host 192.168.1.210 --broker-host 192.168.0.210 --wait
"""
import argparse
import json
import os
import sys
import threading
import time

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from edge_sign import wrap


def repoint(node, ssid, psk, broker, ntp, ota_host, broker_host, broker_port=1883,
            wait=False, timeout=45.0):
    """Publish the signed repoint on the current broker. If wait, watch for the node to go offline
    (LWT) on THIS broker as evidence it rebooted onto the new net. Returns "left" | "sent" | None."""
    inner = {"op": "repoint", "ssid": ssid, "psk": psk, "broker": broker}
    if ntp:
        inner["ntp"] = ntp
    if ota_host:
        inner["ota_host"] = ota_host
    env = wrap(inner)   # {p,s}; edge_sign stamps ts/seq — firmware enforces the tight (300 s) window

    result = {"outcome": None}
    left = threading.Event()
    online_seen = {"val": False}

    def on_connect(c, u, f, rc, props=None):
        c.subscribe(f"home/edge/{node}/status", qos=1)

    def on_message(c, u, msg):
        payload = msg.payload.decode(errors="replace").strip()
        print(f"  status: {payload}")
        if payload.startswith("online"):
            online_seen["val"] = True
        # An empty retained status or an explicit offline LWT after we published = the node left this net.
        elif online_seen["val"] and (payload == "" or "offline" in payload.lower()):
            result["outcome"] = "left"
            left.set()

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if os.environ.get("HA_MQTT_USER"):
        c.username_pw_set(os.environ["HA_MQTT_USER"], os.environ.get("HA_MQTT_PASS"))
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(broker_host, broker_port, 30)
    c.loop_start()

    time.sleep(1.5)   # settle the subscription + learn current online state
    print(f"-> repoint {node}: ssid={ssid} broker={broker} (published on {broker_host})")
    c.publish(f"home/edge/{node}/cmd", json.dumps(env), qos=1)

    if wait:
        left.wait(timeout)
    else:
        time.sleep(2.0)
        result["outcome"] = "sent"
    c.loop_stop()
    c.disconnect()
    return result["outcome"]


def main() -> None:
    p = argparse.ArgumentParser(description="Repoint an ESP32 edge node to a new Wi-Fi + broker (DJ-19)")
    p.add_argument("--node", required=True, help="node_id (home/edge/<node>/cmd)")
    p.add_argument("--ssid", required=True, help="new Wi-Fi SSID (e.g. autohome_airgap)")
    p.add_argument("--psk", default=os.environ.get("WIFI_PSK", ""),
                   help="new Wi-Fi PSK (or $WIFI_PSK; do not put secrets on the command line if avoidable)")
    p.add_argument("--broker", required=True, help="new broker URI, e.g. mqtt://192.168.1.210:1883")
    p.add_argument("--ntp", default="", help="new NTP server (optional; blank = leave unchanged)")
    p.add_argument("--ota-host", default="", help="new OTA host pin (optional; blank = leave unchanged)")
    p.add_argument("--broker-host", default=os.environ.get("HA_BROKER", "localhost"),
                   help="CURRENT broker to publish the command on (where the node is now)")
    p.add_argument("--broker-port", type=int, default=int(os.environ.get("HA_BROKER_PORT", "1883")))
    p.add_argument("--wait", action="store_true",
                   help="watch the current broker for the node to go offline (evidence it left)")
    p.add_argument("--timeout", type=float, default=45.0)
    a = p.parse_args()

    if not a.psk:
        sys.exit("no Wi-Fi PSK — pass --psk or set $WIFI_PSK")
    if not os.environ.get("HA_CMD_SECRET"):
        sys.exit("HA_CMD_SECRET not set — export the node's per-device command secret")

    outcome = repoint(a.node, a.ssid, a.psk, a.broker, a.ntp, a.ota_host,
                      a.broker_host, a.broker_port, wait=a.wait, timeout=a.timeout)
    print()
    if outcome == "left":
        print(f"✓ {a.node} accepted the repoint and left this network — confirm arrival on the new "
              f"broker via device_push (ha-2 API through the bridge).")
    elif outcome == "sent":
        print(f"✓ repoint command published to {a.node}. Not waiting for departure; confirm on the new "
              f"broker via device_push. (Re-run with --wait to watch it drop off here.)")
    else:
        print(f"? {a.node} did not drop off within {a.timeout:.0f}s. It may lack repoint-capable firmware, "
              f"or already left before we subscribed. Verify on the new broker.", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
