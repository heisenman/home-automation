"""
Hot SQLite writer — subscribes to home/+/+/state and persists every reading.

Schema (long format, per §8 of architecture plan):
  readings(id, ts, device_id, device_type, area, transport, metric, value, unit, schema_v)

One row per metric per reading. Columnar Parquet later benefits from this shape.

WAL mode + synchronous=NORMAL: durable on OS crash, fast on power loss (good enough
for sensor data; Parquet cold tier is the long-term durable store).

Usage:
  python3 writer.py --db instance/db/hot.db --broker localhost
"""

import argparse
import json
import logging
import os
import signal
import socket
import sqlite3
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt


def _sd_notify(msg: bytes) -> None:
    """Send a message to the systemd notify socket (no-op if not under systemd)."""
    path = os.environ.get("NOTIFY_SOCKET", "")
    if not path:
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(path)
            s.send(msg)
    except OSError:
        pass

# ── Configuration ─────────────────────────────────────────────────────────────

BROKER_HOST: str = os.environ.get("HA_BROKER", "localhost")
BROKER_PORT: int = int(os.environ.get("HA_BROKER_PORT", "1883"))
SUBSCRIBE_TOPIC: str = "home/+/+/state"

log = logging.getLogger("ha.writer")

# SI units by metric name
_UNITS: dict[str, str] = {
    "temperature_c": "degC",
    "humidity_pct": "%",
    "battery_pct": "%",
    "co2_ppm": "ppm",
    "pressure_hpa": "hPa",
    "radon_bqm3": "Bq/m3",
    "rssi_dbm": "dBm",
}


# ── Database ──────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    device_id   TEXT    NOT NULL,
    device_type TEXT    NOT NULL,
    area        TEXT    NOT NULL,
    transport   TEXT    NOT NULL,
    metric      TEXT    NOT NULL,
    value       REAL    NOT NULL,
    unit        TEXT    NOT NULL,
    schema_v    INTEGER NOT NULL DEFAULT 1
);
-- Idempotency: one row per (device_id, ts, metric). Lets the live writer and any
-- history backfill/re-import use INSERT OR IGNORE so overlapping data never dupes.
-- Same key the compactor dedups on. Also serves (device_id, ts) prefix lookups.
CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_unique ON readings (device_id, ts, metric);
CREATE INDEX IF NOT EXISTS idx_readings_ts        ON readings (ts);
CREATE INDEX IF NOT EXISTS idx_readings_metric    ON readings (metric, device_id);

CREATE TABLE IF NOT EXISTS device_last_seen (
    device_id   TEXT PRIMARY KEY,
    device_type TEXT NOT NULL,
    area        TEXT NOT NULL,
    last_ts     TEXT NOT NULL,
    last_rssi   INTEGER
);
"""


def _open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_DDL)
    conn.commit()
    log.info("Database opened at %s", path)
    return conn


def _insert_readings(conn: sqlite3.Connection, payload: dict) -> int:
    """
    Unpack the metrics dict into individual rows. Returns rows inserted.
    Skips metrics with non-numeric values.
    """
    ts = payload.get("ts", "")
    device_id = payload.get("device_id", "unknown")
    device_type = payload.get("device_type", "unknown")
    area = payload.get("area", "unknown")
    transport = payload.get("transport", "unknown")
    schema_v = payload.get("schema", 1)
    metrics = payload.get("metrics", {})
    meta = payload.get("meta", {})
    rssi = meta.get("rssi")

    rows = []
    for metric, value in metrics.items():
        if not isinstance(value, (int, float)):
            continue
        unit = _UNITS.get(metric, "")
        rows.append((ts, device_id, device_type, area, transport, metric, float(value), unit, schema_v))

    if rows:
        conn.executemany(
            """INSERT OR IGNORE INTO readings (ts, device_id, device_type, area, transport,
               metric, value, unit, schema_v) VALUES (?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.execute(
            """INSERT INTO device_last_seen (device_id, device_type, area, last_ts, last_rssi)
               VALUES (?,?,?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET
                 device_type=excluded.device_type,
                 area=excluded.area,
                 last_ts=excluded.last_ts,
                 last_rssi=excluded.last_rssi""",
            (device_id, device_type, area, ts, rssi),
        )
        conn.commit()

    return len(rows)


# ── MQTT callbacks ─────────────────────────────────────────────────────────────

class Writer:
    def __init__(self, db_path: Path):
        self._conn = _open_db(db_path)
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._running = True

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            log.info("MQTT connected")
            client.subscribe(SUBSCRIBE_TOPIC, qos=1)
            log.info("Subscribed to %s", SUBSCRIBE_TOPIC)
        else:
            log.error("MQTT connect failed rc=%s", rc)

    def _on_disconnect(self, client, userdata, disconnect_flags, rc, properties=None):
        if rc != 0 and self._running:
            log.warning("MQTT disconnected rc=%s — reconnecting", rc)

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage):
        try:
            payload = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.warning("Bad payload on %s: %s", msg.topic, exc)
            return

        try:
            n = _insert_readings(self._conn, payload)
            log.debug("stored %d row(s) from %s", n, msg.topic)
        except sqlite3.Error as exc:
            log.error("DB error on %s: %s", msg.topic, exc)

    def _connect_with_retry(self, host: str, port: int) -> None:
        attempt = 0
        while self._running:
            try:
                self._client.connect(host, port, keepalive=60)
                return
            except Exception as exc:
                attempt += 1
                wait = min(2 ** attempt, 60)
                log.warning("MQTT connect attempt %d failed: %s — retry in %ds", attempt, exc, wait)
                time.sleep(wait)

    def run(self, host: str, port: int) -> None:
        def _stop(*_):
            log.info("Shutting down writer")
            self._running = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        self._connect_with_retry(host, port)
        # Background network thread handles messages + auto-reconnect; the main
        # thread pings the systemd watchdog so we aren't SIGABRT'd every WatchdogSec.
        self._client.loop_start()
        _sd_notify(b"READY=1")
        try:
            while self._running:
                _sd_notify(b"WATCHDOG=1")
                time.sleep(30)
        finally:
            self._client.loop_stop()
            self._client.disconnect()
            self._conn.close()
            log.info("Writer stopped")


# ── Entry point ───────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Home automation SQLite writer")
    p.add_argument("--db", default="instance/db/hot.db", type=Path)
    p.add_argument("--broker", default=BROKER_HOST)
    p.add_argument("--broker-port", default=BROKER_PORT, type=int)
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stdout,
    )
    Writer(args.db).run(args.broker, args.broker_port)


if __name__ == "__main__":
    main()
