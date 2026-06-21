#!/usr/bin/env python3
"""
Trigger an on-device history pull on an edge node.

Publishes a command on  home/edge/<node>/cmd  that the node's firmware acts on: it connects to
the meter over GATT, pulls its history buffer, and streams the raw notifications back on
home/edge/<node>/<mac>/history — where edge_history.py decodes and inserts them.

  python3 tools/edge_pull_history.py --node c6-bench --mac AA:BB:CC:00:00:01 --profile outdoor

profile: meter_pro (Meter / Meter Pro) or outdoor (Outdoor Meter). Look up the meter's
device_type in instance/devices.yaml: switchbot_meter_pro -> meter_pro, *_outdoor -> outdoor.
"""
import argparse
import json
import os

import paho.mqtt.client as mqtt


def main() -> None:
    p = argparse.ArgumentParser(description="Trigger an edge-node history pull")
    p.add_argument("--node", required=True, help="edge node id, e.g. c6-bench")
    p.add_argument("--mac", required=True, help="target meter MAC")
    p.add_argument("--profile", default="outdoor", choices=["meter_pro", "outdoor"])
    p.add_argument("--broker", default=os.environ.get("HA_BROKER", "localhost"))
    p.add_argument("--broker-port", type=int, default=int(os.environ.get("HA_BROKER_PORT", "1883")))
    a = p.parse_args()

    cmd = {"op": "history", "mac": a.mac.upper(), "profile": a.profile}
    topic = f"home/edge/{a.node}/cmd"
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.connect(a.broker, a.broker_port, 30)
    c.loop_start()
    info = c.publish(topic, json.dumps(cmd), qos=1)
    info.wait_for_publish(timeout=10)
    c.loop_stop()
    c.disconnect()
    print(f"sent -> {topic}: {cmd}")


if __name__ == "__main__":
    main()
