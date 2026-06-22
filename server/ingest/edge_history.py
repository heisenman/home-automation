"""
Edge history ingest — decode on-device-history streams relayed by an edge node.

A C6 edge node can pull a SwitchBot meter's on-device history buffer over GATT (the protocol
in tools/switchbot_history.py) and relay the RAW notifications up — it does the BLE transport,
the dictator does the authoritative decode. This service reassembles a pull and inserts it,
reusing the exact decode/timestamp/re-anchor code that the server-side puller uses.

Wire format (published by the node on  home/edge/<node>/<mac>/history ):
  {"t":"meta","mac":..,"profile":..,"newest_ts":..,"newest_ptr":..,"oldest_ts":..,
   "oldest_ptr":..,"start_addr":..,"pull_now":..}
  {"t":"data","mac":..,"seq":k,"notifs":["<hex>",...]}     # batches of record notifications
  {"t":"done","mac":..,"count":N}                          # N = total record notifications sent

Idempotent: inserts via INSERT OR IGNORE on UNIQUE(device_id, ts, metric), same as the puller.
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

# reuse the proven decode/timestamp/insert code from the server-side puller
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "tools"))
import switchbot_history as sbh   # noqa: E402

from server.ingest.edge_mapper import load_registry, _utc_now  # noqa: E402
from server.util.mqtt_creds import apply_credentials  # noqa: E402

log = logging.getLogger("ha.edge_history")

BROKER_HOST = os.environ.get("HA_BROKER", "localhost")
BROKER_PORT = int(os.environ.get("HA_BROKER_PORT", "1883"))
SUBSCRIBE_TOPIC = "home/edge/+/+/history"
DB_PATH = Path(os.environ.get("HA_DB", "instance/db/hot.db"))


class _Session:
    """Accumulates one in-flight pull for a (node, mac)."""
    __slots__ = ("meta", "notifs", "started")

    def __init__(self):
        self.meta = None
        self.notifs: list[bytes] = []
        self.started = time.monotonic()


class HistoryIngest:
    def __init__(self, registry, db: Path):
        self._registry = registry
        self._db = db
        self._sessions: dict[str, _Session] = {}   # key: "node/mac"

    def on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            client.subscribe(SUBSCRIBE_TOPIC, qos=1)
            log.info("connected; subscribed to %s", SUBSCRIBE_TOPIC)
        else:
            log.error("connect failed rc=%s", rc)

    def on_message(self, client, userdata, msg):
        try:
            m = json.loads(msg.payload.decode())
        except (ValueError, UnicodeDecodeError) as exc:
            log.warning("bad history payload on %s: %s", msg.topic, exc)
            return
        parts = msg.topic.split("/")          # home/edge/<node>/<mac>/history
        node = parts[2] if len(parts) > 3 else "?"
        mac = str(m.get("mac", "")).upper()
        key = f"{node}/{mac}"
        t = m.get("t")

        if t == "meta":
            self._sessions[key] = _Session()
            self._sessions[key].meta = m
            log.info("[%s] pull start: ptr %s..%s, %d record-addrs",
                     key, m.get("oldest_ptr"), m.get("newest_ptr"),
                     (m.get("newest_ptr", 0) - m.get("start_addr", 0)))
        elif t == "data":
            s = self._sessions.get(key)
            if not s:
                return  # data before meta — ignore
            for hexstr in m.get("notifs", []):
                try:
                    s.notifs.append(bytes.fromhex(hexstr))
                except ValueError:
                    pass
        elif t == "done":
            self._finish(key, m)

    def _finish(self, key, m):
        s = self._sessions.pop(key, None)
        if not s or not s.meta:
            log.warning("[%s] done without a session/meta", key)
            return
        node, mac = key.split("/", 1)
        reg = self._registry.get(mac)
        if not reg:
            log.warning("[%s] unknown mac — not inserting (add to registry)", key)
            return

        meta = {k: s.meta.get(k) for k in
                ("newest_ts", "newest_ptr", "oldest_ts", "oldest_ptr", "start_addr", "pull_now")}
        samples = sbh.decode_meter_pro(s.notifs)
        sbh.reanchor_to_now(meta, enabled=True)          # correct drifted device clocks
        is_outdoor = "outdoor" in (reg.get("device_type") or "").lower()
        if is_outdoor and samples and meta.get("newest_ts"):
            # Outdoor history banks can WRAP, so the paged records don't begin at start_addr and the
            # address->index mapping in assign_timestamps slides the timestamps. The last relayed record
            # IS the newest, so anchor it to newest_ts (already re-anchored to ~now) and count backward at
            # the interval — robust regardless of where the records physically sit (h_bed bank 3).
            np_, op, ot = meta.get("newest_ptr"), meta.get("oldest_ptr"), meta.get("oldest_ts")
            interval = ((meta["newest_ts"] - ot) / (np_ - op)
                        if ot is not None and np_ and op is not None and np_ != op else 60.0)
            if not 20 <= interval <= 3600:
                interval = 60.0
            nt, n = meta["newest_ts"], len(samples)
            tsamples = [(int(round(nt - (n - 1 - k) * interval)), t, h) for k, (t, h) in enumerate(samples)]
        else:
            tsamples = sbh.assign_timestamps(samples, meta)
        if not tsamples:
            log.warning("[%s] decoded %d notifs -> 0 timestamped samples (bad meta?)", key, len(s.notifs))
            return
        # safety: newest sample must be ~now (same guard as the server-side puller)
        skew = abs(time.time() - tsamples[-1][0])
        if skew > 3600:
            log.error("[%s] newest sample %.0fs from now — refusing insert", key, skew)
            return
        n = sbh.insert_samples(self._db, reg["device_id"],
                               reg.get("device_type", "unknown"), reg.get("area", "unknown"), tsamples)
        log.info("[%s] %d notifs -> %d samples -> inserted %d new rows (%s)",
                 key, len(s.notifs), len(tsamples), n, reg["device_id"])


def main() -> None:
    p = argparse.ArgumentParser(description="Edge on-device-history ingest")
    p.add_argument("--registry", default="instance/devices.yaml", type=Path)
    p.add_argument("--db", default=DB_PATH, type=Path)
    p.add_argument("--broker", default=BROKER_HOST)
    p.add_argument("--broker-port", default=BROKER_PORT, type=int)
    p.add_argument("--log-level", default="INFO")
    a = p.parse_args()
    logging.basicConfig(level=getattr(logging, a.log_level),
                        format="%(asctime)s %(levelname)s %(name)s — %(message)s", stream=sys.stdout)

    registry = load_registry(a.registry)
    log.info("registry loaded: %d devices; db=%s", len(registry), a.db)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    apply_credentials(client)
    ing = HistoryIngest(registry, a.db)
    client.on_connect = ing.on_connect
    client.on_message = ing.on_message
    attempt = 0
    while True:
        try:
            client.connect(a.broker, a.broker_port, keepalive=60)
            break
        except Exception as exc:
            attempt += 1
            wait = min(2 ** attempt, 60)
            log.warning("connect attempt %d failed: %s — retry in %ds", attempt, exc, wait)
            time.sleep(wait)
    client.loop_forever()


if __name__ == "__main__":
    main()
