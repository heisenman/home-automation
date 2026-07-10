#!/usr/bin/env python3
"""relay-weather-client — ha-2 (air-gapped) pulls weather via the relay and stores it (ADR-0033 Phase 2).

Request/response, dictator-initiated (Q4): ha-2 asks the relay on .210 for weather when a timer fires (with
jitter, so egress is stochastic — traffic-analysis resistance), gets the answer back, writes it to its own
`weather.db`. ha-2 NEVER imports the fetch code and NEVER reaches the internet — it only talks to the relay
broker on the air-gap net. Self-contained: paho + stdlib sqlite3 only (no server.weather import).

Env (see instance/relay.env):
  RELAY_BROKER_HOST (192.168.1.245)  RELAY_BROKER_PORT (1884)
  RELAY_BROKER_USER (ha2)            RELAY_BROKER_PASS
  HA_WEATHER_DB (instance/db/weather.db)  HA_WEATHER_SOURCE (openmeteo)  HA_WEATHER_LOCATION (home)
  RELAY_TIMEOUT_S (20)
"""
from __future__ import annotations

import os
import sys
import json
import time
import uuid
import sqlite3
import logging
from pathlib import Path

import paho.mqtt.client as mqtt

LOG = logging.getLogger("relay-weather")

_UNITS = {"temperature_c": "degC", "humidity_pct": "%", "pressure_hpa": "hPa"}
_METRICS = ("temperature_c", "humidity_pct", "pressure_hpa")

DDL = """
CREATE TABLE IF NOT EXISTS weather (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, source TEXT NOT NULL,
    location TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL,
    schema_v INTEGER NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weather_unique ON weather (source, location, ts, metric);
CREATE INDEX IF NOT EXISTS idx_weather_ts ON weather (ts);
"""


def _norm_ts(ts: str) -> str:
    """open-meteo returns 'YYYY-MM-DDTHH:MM' — normalize to the '...:00Z' shape .210 stores."""
    if not ts:
        return ts
    if len(ts) == 16 and ts[10] == "T":  # YYYY-MM-DDTHH:MM
        return ts + ":00Z"
    if not ts.endswith("Z"):
        return ts + "Z"
    return ts


def _store(db: Path, source: str, location: str, ts: str, data: dict) -> int:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(DDL)
        rows = [
            (ts, source, location, m, float(data[m]), _UNITS.get(m, ""), 1)
            for m in _METRICS if data.get(m) is not None
        ]
        if not rows:
            return 0
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO weather (ts,source,location,metric,value,unit,schema_v) "
            "VALUES (?,?,?,?,?,?,?)", rows)
        conn.commit()
        return conn.total_changes - before
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = os.environ.get("RELAY_BROKER_HOST", "192.168.1.245")
    port = int(os.environ.get("RELAY_BROKER_PORT", "1884"))
    user = os.environ.get("RELAY_BROKER_USER", "ha2")
    pw = os.environ.get("RELAY_BROKER_PASS", "")
    db = Path(os.environ.get("HA_WEATHER_DB", "instance/db/weather.db"))
    source = os.environ.get("HA_WEATHER_SOURCE", "openmeteo")
    location = os.environ.get("HA_WEATHER_LOCATION", "home")
    timeout = float(os.environ.get("RELAY_TIMEOUT_S", "20"))

    rid = str(uuid.uuid4())
    result: dict = {}
    done = threading_event()

    def on_connect(c, *_a):
        c.subscribe("relay/weather/response", qos=1)
        c.publish("relay/weather/request", json.dumps({"id": rid}), qos=1)

    def on_message(_c, _u, msg):
        try:
            m = json.loads(msg.payload)
        except Exception:
            return
        if m.get("id") != rid:
            return  # not our request
        result.update(m)
        done.set()

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"relay-weather-{rid[:8]}")
    except (AttributeError, TypeError):
        client = mqtt.Client(client_id=f"relay-weather-{rid[:8]}")
    if user:
        client.username_pw_set(user, pw)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, keepalive=30)
    client.loop_start()
    got = done.wait(timeout)
    client.loop_stop()
    client.disconnect()

    if not got:
        LOG.error("no relay response within %ss", timeout)
        return 2
    if not result.get("ok"):
        LOG.error("relay error: %s", result.get("error"))
        return 3
    data = result.get("data") or {}
    ts = _norm_ts(data.get("ts") or "")
    added = _store(db, source, location, ts, data)
    LOG.info("weather stored ts=%s added=%d (temp=%s rh=%s p=%s)",
             ts, added, data.get("temperature_c"), data.get("humidity_pct"), data.get("pressure_hpa"))
    return 0


def threading_event():
    import threading
    return threading.Event()


if __name__ == "__main__":
    sys.exit(main())
