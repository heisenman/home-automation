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
from server.util.registry_reload import RegistryReloader  # noqa: E402
from server.util.mqtt_creds import apply_credentials  # noqa: E402
try:
    from server.mesh import store as mesh_store  # noqa: E402
    _MESH_OK = True
except Exception:                                 # pragma: no cover
    _MESH_OK = False

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
        self._registry_get = lambda: registry     # swapped for a live source by attach_reloader()
        self._db = db
        self._sessions: dict[str, _Session] = {}   # key: "node/mac"

    def attach_reloader(self, reloader) -> "HistoryIngest":
        """Swap the static registry for a live mtime-reloading source so a device relocate
        (devices.yaml edit) takes effect without a restart (relocate-ingest-reload)."""
        self._registry_get = reloader.current
        return self

    @property
    def _registry(self) -> dict:
        return self._registry_get()

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
        raw_newest_ts = meta.get("newest_ts")            # device-clock ts BEFORE reanchor (frozen-buffer guard)
        sbh.reanchor_to_now(meta, enabled=True)          # correct drifted device clocks
        # Both Outdoor (multi-bank, can WRAP) and current-firmware Meter Pro report only a single write
        # pointer; the node windows back from it (oldest is synthesized, not a real second pointer). In both
        # cases the LAST relayed record is the newest, so anchor it to newest_ts (already re-anchored to
        # ~now) and count backward at the interval — robust to the address->sample-index slide that would
        # otherwise mistime assign_timestamps' output (decode yields multiple samples per address unit).
        if samples and meta.get("newest_ts"):
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
            self._record_pull(reg["device_id"], node, ok=False, n=0, reason="no_metadata")
            return
        # safety: newest sample must be ~now (same guard as the server-side puller)
        skew = abs(time.time() - tsamples[-1][0])
        if skew > 3600:
            log.error("[%s] newest sample %.0fs from now — refusing insert", key, skew)
            self._record_pull(reg["device_id"], node, ok=False, n=0, reason="skew")
            return

        # frozen-buffer guard: a dead meter (e.g. clock stopped, as seen on meter_pro_c_office 2026-07-26)
        # keeps returning the SAME write pointer + the same old records; reanchoring them to "now" injects
        # stale data as current. Refuse when the pointer hasn't advanced since the last pull; and for a
        # large-drift FIRST pull (no prior pointer) when the newest record disagrees with the live reading.
        did = reg["device_id"]
        newest_ptr = meta.get("newest_ptr")
        last_ptr, _ = self._last_pull_ptr(did)
        if newest_ptr is not None and last_ptr is not None and newest_ptr == last_ptr:
            log.warning("[%s] write pointer static at %s since last pull — buffer not advancing "
                        "(frozen meter or no new data); refusing insert", key, newest_ptr)
            self._record_pull(did, node, ok=False, n=0, reason="ptr_static")
            return
        raw_drift_h = abs(time.time() - raw_newest_ts) / 3600.0 if raw_newest_ts else 0.0
        if last_ptr is None and raw_drift_h > 2.0:
            adv = self._latest_adv(did)
            nt_temp, nt_hum = tsamples[-1][1], tsamples[-1][2]
            bad = (("temperature_c" in adv and abs(nt_temp - adv["temperature_c"]) > 1.0)
                   or ("humidity_pct" in adv and abs(nt_hum - adv["humidity_pct"]) > 3.0))
            if adv and bad:
                log.warning("[%s] first pull, device clock %.0fh behind; newest history %.1f°C/%d%% disagrees "
                            "with live %s°C/%s%% — likely frozen buffer; refusing insert", key, raw_drift_h,
                            nt_temp, nt_hum, adv.get("temperature_c"), adv.get("humidity_pct"))
                self._record_pull(did, node, ok=False, n=0, reason="stale_vs_live")
                self._record_pull_ptr(did, newest_ptr, raw_newest_ts)   # remember it so re-pulls are caught too
                return

        n = sbh.insert_samples(self._db, reg["device_id"],
                               reg.get("device_type", "unknown"), reg.get("area", "unknown"), tsamples)
        self._record_pull_ptr(did, newest_ptr, raw_newest_ts)
        log.info("[%s] %d notifs -> %d samples -> inserted %d new rows (%s)",
                 key, len(s.notifs), len(tsamples), n, reg["device_id"])
        self._record_pull(reg["device_id"], node, ok=True, n=n, reason="")

    def _ensure_ptr_table(self, c) -> None:
        c.execute("CREATE TABLE IF NOT EXISTS history_pull_ptr "
                  "(device_id TEXT PRIMARY KEY, newest_ptr INTEGER, raw_newest_ts INTEGER, updated TEXT)")

    def _last_pull_ptr(self, device_id):
        """Last (newest_ptr, raw_newest_ts) recorded for this device, or (None, None). Frozen-buffer guard."""
        import sqlite3
        try:
            c = sqlite3.connect(str(self._db))
            self._ensure_ptr_table(c)
            row = c.execute("SELECT newest_ptr, raw_newest_ts FROM history_pull_ptr WHERE device_id=?",
                            (device_id,)).fetchone()
            c.close()
            return (row[0], row[1]) if row else (None, None)
        except Exception as exc:                       # pragma: no cover
            log.debug("pull_ptr read failed: %s", exc)
            return (None, None)

    def _record_pull_ptr(self, device_id, newest_ptr, raw_newest_ts) -> None:
        import sqlite3
        try:
            c = sqlite3.connect(str(self._db))
            self._ensure_ptr_table(c)
            c.execute("INSERT INTO history_pull_ptr(device_id,newest_ptr,raw_newest_ts,updated) VALUES(?,?,?,?) "
                      "ON CONFLICT(device_id) DO UPDATE SET newest_ptr=excluded.newest_ptr, "
                      "raw_newest_ts=excluded.raw_newest_ts, updated=excluded.updated",
                      (device_id, newest_ptr, raw_newest_ts, _utc_now()))
            c.commit()
            c.close()
        except Exception as exc:                       # pragma: no cover
            log.debug("pull_ptr write failed: %s", exc)

    def _latest_adv(self, device_id):
        """Latest live advertised temp/hum (last 30 min) to sanity-check a large-drift first pull."""
        import sqlite3
        out = {}
        try:
            c = sqlite3.connect(f"file:{self._db}?mode=ro", uri=True)
            for metric in ("temperature_c", "humidity_pct"):
                r = c.execute("SELECT value FROM readings WHERE device_id=? AND metric=? AND transport='ble-adv' "
                              "AND ts > datetime('now','-30 minutes') ORDER BY ts DESC LIMIT 1",
                              (device_id, metric)).fetchone()
                if r:
                    out[metric] = r[0]
            c.close()
        except Exception as exc:                       # pragma: no cover
            log.debug("latest_adv read failed: %s", exc)
        return out

    def _record_pull(self, device_id, node, ok: bool, n: int, reason: str) -> None:
        """Append this edge pull's outcome to pull_log so the mesh router learns which node can pull
        which endpoint (terminal receiver = the relaying node). Best-effort — never breaks ingest."""
        if not _MESH_OK:
            return
        try:
            import sqlite3
            c = sqlite3.connect(str(self._db))
            mesh_store.ensure_schema(c)
            mesh_store.record_pull(c, device_id, f"server:server>node:{node}>{device_id}",
                                   ok=ok, n_samples=n, reason=reason)
            c.close()
        except Exception as exc:
            log.debug("pull_log record failed: %s", exc)


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
    ing.attach_reloader(RegistryReloader(a.registry, load_registry, logger=log))
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
