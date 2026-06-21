#!/usr/bin/env python3
"""
Simulated actuator over MQTT — a hardware-free stand-in that ENFORCES the command handshake.

Subscribes to its command topic, verifies each command's per-device signature / nonce / freshness
(server.control.protocol.Verifier), validates the action against its declared traits, applies it to
in-memory state, and publishes an ack + retained state. Forged / tampered / stale / replayed /
out-of-contract commands are refused with a reason. This mirrors what the real node firmware will do.

  home/<area>/<device_id>/cmd       <- signed commands from the dictator (PEP)
  home/<area>/<device_id>/cmd/ack   -> ack {id,status,reason,reported_state,source}
  home/<area>/<device_id>/state     -> retained reported state

Example:
  python3 tools/sim_device.py --device lamp_office --area c_office \
      --traits switchable,ranged --secret secret-lamp-BBB --broker 192.168.0.245
"""
import argparse
import json
import os
import sys

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from server.control.sim import SimActuator


def main() -> None:
    p = argparse.ArgumentParser(description="Simulated MQTT actuator (enforces the command handshake)")
    p.add_argument("--device", required=True)
    p.add_argument("--area", required=True)
    p.add_argument("--traits", required=True, help="comma list, e.g. switchable,ranged")
    p.add_argument("--secret", required=True, help="this device's per-device secret")
    p.add_argument("--broker", default=os.environ.get("HA_BROKER", "localhost"))
    p.add_argument("--broker-port", type=int, default=int(os.environ.get("HA_BROKER_PORT", "1883")))
    a = p.parse_args()

    traits_cfg = {t.strip(): {} for t in a.traits.split(",") if t.strip()}
    act = SimActuator(a.device, traits_cfg, a.secret)
    base = f"home/{a.area}/{a.device}"
    cmd_topic, ack_topic, state_topic = f"{base}/cmd", f"{base}/cmd/ack", f"{base}/state"

    def on_connect(c, u, f, rc, props=None):
        c.subscribe(cmd_topic, qos=1)
        c.publish(state_topic, json.dumps(act._report()), qos=1, retain=True)
        print(f"[{a.device}] online; traits={list(traits_cfg)}; state={act._report()}")
        print(f"[{a.device}] listening on {cmd_topic}")

    def on_message(c, u, m):
        try:
            cmd = json.loads(m.payload.decode())
        except Exception:
            return
        ack = act.handle_command(cmd)
        c.publish(ack_topic, json.dumps(ack), qos=1)
        c.publish(state_topic, json.dumps(act._report()), qos=1, retain=True)
        flag = "OK  " if ack["status"] == "ok" else "DENY"
        print(f"[{a.device}] {flag} id={ack['id']} reason={ack['reason']} state={ack['reported_state']}")

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(a.broker, a.broker_port, 30)
    try:
        c.loop_forever()
    except KeyboardInterrupt:
        print(f"\n[{a.device}] shutting down")


if __name__ == "__main__":
    main()
