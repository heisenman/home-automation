#!/usr/bin/env python3
"""
Generic GATT forwarder — drive ANY BLE-GATT interaction through an edge node, no new firmware.

The server composes a list of BLE *steps*; the node connects to the target, discovers all its
characteristics, runs the steps in order, and streams replies back. This is the generalization of
edge_pull_history.py: the node is a dumb GATT proxy, all device-specific logic lives here.

Wire protocol
-------------
  publish  home/edge/<node>/cmd            {"op":"gatt","reqid":"..","mac":"..","steps":[...]}
  reply    home/edge/<node>/<reqid>/reply  one JSON message per event:
      {"t":"open","mac":..,"chrs":[{"u":<uuid>,"h":<handle>},..]}   after discovery
      {"t":"step","i":<idx>,"op":..,"h":..,"rc":0}                  write/sub ack
      {"t":"read","i":<idx>,"h":..,"rc":0,"d":"<hex>"}              read result
      {"t":"notif","seq":N,"items":[[<handle>,"<hex>"],..]}         batched notifications
      {"t":"done","status":0,"notifs":<count>}                     terminal
      {"t":"error","msg":".."}                                     non-terminal error

Steps (each a JSON object with "s")
-----------------------------------
  {"s":"sub","char":"<uuid>"}                              subscribe (notifications on)
  {"s":"write","char":"<uuid>","hex":"570f.."}             one write
  {"s":"writeseq","char":"<uuid>","hex":["..",".."],"gap_ms":300}   sequence of writes
  {"s":"read","char":"<uuid>"}                             read, result returned as {"t":"read"}
  {"s":"collect","ms":2000}                                dwell, gathering notifications
  {"s":"delay","ms":300}                                   pause

UUIDs may be 16-bit ("2a00") or full 128-bit canonical form.

CLI examples
------------
  # Probe: connect + discover all characteristics (empty step list)
  python3 tools/edge_gatt.py --node c6-bench --mac AA:BB:CC:00:00:03

  # Read the Device Name characteristic (0x2a00)
  python3 tools/edge_gatt.py --node c6-bench --mac <mac> \
      --steps '[{"s":"read","char":"2a00"}]'

  # Arbitrary composed interaction from a file
  python3 tools/edge_gatt.py --node c6-bench --mac <mac> --steps-file steps.json
"""
import argparse
import json
import os
import sys
import threading
import time

import paho.mqtt.client as mqtt


def edge_gatt(node, mac, steps, broker="localhost", port=1883, timeout=60.0,
              reqid=None, on_event=None):
    """Run a generic GATT step-list on an edge node; return the list of reply events.

    Blocks until a {"t":"done"} reply or `timeout` seconds elapse. `on_event(evt)` is called for
    each reply as it arrives (for streaming/progress)."""
    if reqid is None:
        # short, topic-safe, unique enough for concurrent runs (fits firmware's 24-char buffer)
        reqid = f"{int(time.time()) % 100000:05d}{os.getpid() % 1000:03d}"
    reply_topic = f"home/edge/{node}/{reqid}/reply"
    cmd_topic = f"home/edge/{node}/cmd"
    from edge_sign import wrap
    cmd = wrap({"op": "gatt", "reqid": reqid, "mac": mac.upper(), "steps": steps})

    events = []
    done = threading.Event()

    def on_connect(c, u, flags, rc, props=None):
        c.subscribe(reply_topic, qos=1)
        # publish only after the reply subscription is in place (avoid racing the first reply)
        c.publish(cmd_topic, json.dumps(cmd), qos=1)

    def on_message(c, u, msg):
        try:
            evt = json.loads(msg.payload.decode())
        except Exception:
            return
        events.append(evt)
        if on_event:
            on_event(evt)
        if evt.get("t") == "done":
            done.set()

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if os.environ.get("HA_MQTT_USER"):   # broker auth: admin tools connect as the dictator
        c.username_pw_set(os.environ["HA_MQTT_USER"], os.environ.get("HA_MQTT_PASS"))
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(broker, port, 30)
    c.loop_start()
    done.wait(timeout)
    c.loop_stop()
    c.disconnect()
    return events, reqid


def _print_event(evt):
    t = evt.get("t")
    if t == "open":
        print(f"  [open] {evt.get('mac')} — {len(evt.get('chrs', []))} characteristics:")
        for ch in evt.get("chrs", []):
            print(f"         h={ch['h']:<5} {ch['u']}")
    elif t == "notif":
        for h, hexs in evt.get("items", []):
            print(f"  [notif] h={h} {hexs}")
    elif t == "read":
        print(f"  [read]  i={evt.get('i')} h={evt.get('h')} rc={evt.get('rc')} d={evt.get('d')}")
    elif t == "step":
        print(f"  [step]  i={evt.get('i')} {evt.get('op')} h={evt.get('h')} rc={evt.get('rc')}")
    elif t == "done":
        print(f"  [done]  status={evt.get('status')} notifs={evt.get('notifs')}")
    elif t == "error":
        print(f"  [error] {evt.get('msg')}")
    else:
        print(f"  {evt}")


def main() -> None:
    p = argparse.ArgumentParser(description="Generic GATT forwarder via an edge node")
    p.add_argument("--node", required=True, help="edge node id, e.g. c6-bench")
    p.add_argument("--mac", required=True, help="target device MAC")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--steps", help="JSON array of steps")
    g.add_argument("--steps-file", help="file containing a JSON array of steps")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--broker", default=os.environ.get("HA_BROKER", "localhost"))
    p.add_argument("--broker-port", type=int, default=int(os.environ.get("HA_BROKER_PORT", "1883")))
    p.add_argument("--json", action="store_true", help="dump raw events as JSON instead of pretty-printing")
    a = p.parse_args()

    if a.steps_file:
        with open(a.steps_file) as f:
            steps = json.load(f)
    elif a.steps:
        steps = json.loads(a.steps)
    else:
        steps = []   # empty = probe (connect + discover only)

    if not isinstance(steps, list):
        print("steps must be a JSON array", file=sys.stderr)
        sys.exit(2)

    print(f"-> {a.node} mac={a.mac} steps={len(steps)} (probe)" if not steps
          else f"-> {a.node} mac={a.mac} steps={len(steps)}")
    events, reqid = edge_gatt(
        a.node, a.mac, steps, broker=a.broker, port=a.broker_port, timeout=a.timeout,
        on_event=None if a.json else _print_event,
    )
    if a.json:
        print(json.dumps(events, indent=2))
    if not any(e.get("t") == "done" for e in events):
        print(f"(!) no 'done' reply within {a.timeout}s (reqid={reqid})", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
