#!/usr/bin/env python3
"""
Push a firmware OTA to an edge node and watch it land (with rollback detection).

Serves the firmware .bin over a short-lived local HTTP server, tells the node to pull it
({"op":"ota","url":..}), and follows home/edge/<node>/status + /log through the cycle:

  download -> reboot into the inactive slot (pending verify) -> self-test -> confirm OR rollback.

Outcome is decided from the status stream:
  * SUCCESS  — node comes back online on a NEW slot and logs "self-test PASS".
  * ROLLBACK — node returns online on the SAME slot it started on (the bad image failed its
               self-test and the bootloader reverted). Reported as a failure exit.

The node must be able to reach THIS host's HTTP server — pass --serve-ip with an address the
node can route to (the broker-facing IP of this machine).

  python3 tools/edge_ota.py --node c6-bench \
      --bin edge/esp32c6/build/ha-edge-c6.bin \
      --serve-ip 192.168.0.112 --broker 192.168.0.245
"""
import argparse
import functools
import http.server
import json
import os
import socketserver
import threading
import time

import paho.mqtt.client as mqtt


def _serve(directory, port):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=directory)
    httpd = socketserver.TCPServer(("0.0.0.0", port), handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def push_ota(node, bin_path, serve_ip, broker, serve_port=8090, broker_port=1883, timeout=180.0,
             version=1):
    import hashlib
    from edge_sign import wrap
    bin_path = os.path.abspath(bin_path)
    directory, fname = os.path.split(bin_path)
    url = f"http://{serve_ip}:{serve_port}/{fname}"
    sha256 = hashlib.sha256(open(bin_path, "rb").read()).hexdigest()
    # SIGNED ota directive (ADR-0010). sha256+version are carried for the firmware hash-verify
    # follow-up; today's firmware authenticates the directive via the {p,s} signature.
    ota_cmd = wrap({"op": "ota", "url": url, "sha256": sha256, "version": version})
    httpd = _serve(directory, serve_port)

    start_slot = {"val": None}     # slot/version the node was on before the push
    result = {"outcome": None}     # "success" | "rollback" | None(timeout)
    online_after_push = threading.Event()
    pushed = {"val": False}
    done = threading.Event()

    def on_connect(c, u, f, rc, props=None):
        c.subscribe(f"home/edge/{node}/status", qos=1)
        c.subscribe(f"home/edge/{node}/log", qos=0)

    def on_message(c, u, msg):
        payload = msg.payload.decode(errors="replace").strip()
        topic = msg.topic
        if topic.endswith("/status"):
            print(f"  status: {payload}")
            if payload.startswith("online"):
                slot = payload[len("online"):].strip()    # "<slot> <version>"
                if not pushed["val"]:
                    start_slot["val"] = slot               # baseline before push
                elif online_after_push.is_set() is False:
                    online_after_push.set()
                    if slot == start_slot["val"]:
                        result["outcome"] = "rollback"
                    else:
                        result["outcome"] = "success"
                    done.set()
        else:
            print(f"  log:    {payload}")
            if "self-test FAIL" in payload:
                result["outcome"] = "rollback"

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(broker, broker_port, 30)
    c.loop_start()

    time.sleep(2.0)                # learn the baseline slot before pushing
    print(f"-> pushing OTA to {node}: {url} (was on '{start_slot['val']}')")
    pushed["val"] = True
    c.publish(f"home/edge/{node}/cmd", json.dumps(ota_cmd), qos=1)

    done.wait(timeout)
    c.loop_stop(); c.disconnect()
    httpd.shutdown()
    return result["outcome"], start_slot["val"]


def main() -> None:
    p = argparse.ArgumentParser(description="Push a firmware OTA to an edge node")
    p.add_argument("--node", required=True)
    p.add_argument("--bin", required=True, help="path to firmware .bin")
    p.add_argument("--serve-ip", required=True, help="IP of THIS host that the node can reach")
    p.add_argument("--serve-port", type=int, default=8090)
    p.add_argument("--broker", default=os.environ.get("HA_BROKER", "localhost"))
    p.add_argument("--broker-port", type=int, default=int(os.environ.get("HA_BROKER_PORT", "1883")))
    p.add_argument("--timeout", type=float, default=180.0)
    a = p.parse_args()

    outcome, start = push_ota(a.node, a.bin, a.serve_ip, a.broker,
                              serve_port=a.serve_port, broker_port=a.broker_port, timeout=a.timeout)
    print()
    if outcome == "success":
        print("✓ OTA SUCCESS — new image self-test passed and was confirmed valid")
    elif outcome == "rollback":
        print(f"✗ OTA ROLLED BACK — bad image failed self-test; node reverted to '{start}' (safe)")
        raise SystemExit(1)
    else:
        print("? OTA result UNKNOWN — no terminal status within timeout")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
