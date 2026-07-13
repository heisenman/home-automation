#!/usr/bin/env python3
"""Mesh-wide SwitchBot battery refresh — nightly sweep + manual trigger.

Loads the master-decrypted node-secret LUT, connects to the broker, and fires a signed batt_refresh at
every enrolled edge node (staggered by default). Wired to run once nightly by ha-battery-refresh.timer in
the 01:00–05:00 quiet window; also runnable by hand. The on-demand PWA path lives in the API, not here.

  # nightly (systemd) / manual full sweep:
  HA_MASTER_PASS_FILE=instance/.master_pass python3 tools/battery_refresh.py --broker 127.0.0.1
  # a subset, snappier window, no stagger:
  python3 tools/battery_refresh.py --broker 192.168.1.200 --nodes hoffice_c6,s3-crawlspace --stagger 0
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paho.mqtt.client as mqtt  # noqa: E402

from server.control.secret_store import load_lut, load_master  # noqa: E402
from server.mesh.battery_refresh import DEFAULT_STAGGER_S, DEFAULT_WINDOW_S, sweep  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="mesh-wide SwitchBot battery refresh sweep")
    p.add_argument("--broker", default="127.0.0.1", help="broker host the edge fleet is on (nightly: local)")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--window", type=int, default=DEFAULT_WINDOW_S, help="active-scan window per node (s)")
    p.add_argument("--stagger", type=float, default=DEFAULT_STAGGER_S, help="gap between nodes (s); 0 = all at once")
    p.add_argument("--lut", default="instance/node_secrets.enc")
    p.add_argument("--nodes", default="", help="comma-separated subset (default: all enrolled)")
    a = p.parse_args()

    lut = load_lut(a.lut, load_master())
    if not lut:
        print("battery_refresh: empty LUT — no enrolled nodes / no master", file=sys.stderr)
        return 2
    nodes = [n.strip() for n in a.nodes.split(",") if n.strip()] or None

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.connect(a.broker, a.port, 30)
    c.loop_start()
    sent = sweep(lambda t, pl: c.publish(t, pl, qos=1), lut,
                 window_s=a.window, stagger_s=a.stagger, nodes=nodes)
    time.sleep(1.0)   # let the last publish flush
    c.loop_stop()
    c.disconnect()
    print(f"battery_refresh: commanded {len(sent)}/{len(nodes or lut)} node(s): {', '.join(sent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
