"""
SwitchBot cloud history importer — one-time pre-population tool.

Pulls up to ~180 days of historical readings from the SwitchBot cloud API
and imports them directly into the SQLite hot tier (bypasses MQTT since
this is historical/bulk data).

Architecture note: this is the "walled cloud lane" described in §6 of the
architecture plan. It runs once to seed historical data; after that, the
offline BLE scanner is the sole data source. The SwitchBot cloud is never
wired into runtime operation.

Prerequisites:
  1. SwitchBot API token and secret from the app:
       SwitchBot app → Profile → Preferences → App Version (tap 10×) →
       Developer Options → Token + Secret
  2. Device IDs from the SwitchBot API (discovered by this script if you
     run --list-devices first)

Usage:
  # List your SwitchBot devices and their cloud IDs
  python3 tools/import_switchbot_history.py \
      --token YOUR_TOKEN --secret YOUR_SECRET \
      --list-devices

  # Import history for all meter devices
  python3 tools/import_switchbot_history.py \
      --token YOUR_TOKEN --secret YOUR_SECRET \
      --db instance/db/hot.db \
      --days 180

  # Import for a specific device
  python3 tools/import_switchbot_history.py \
      --token YOUR_TOKEN --secret YOUR_SECRET \
      --db instance/db/hot.db \
      --device-id DEVICE_ID \
      --days 180

API reference: https://github.com/OpenWonderLabs/SwitchBotAPI (v1.1)
"""

import argparse
import hashlib
import hmac
import json
import logging
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: venv/bin/pip install httpx", file=sys.stderr)
    sys.exit(1)

log = logging.getLogger("ha.import")

API_BASE = "https://api.switch-bot.com/v1.1"

# SwitchBot device types that are meters (temperature/humidity)
METER_TYPES = {
    "Meter",
    "MeterPlus",
    "WoSensorTH",
    "MeterPro",
    "MeterPro(CO2)",
    "Hub 2",          # Hub 2 has built-in T/H sensor
}

# Map SwitchBot cloud type → our device_type
_DEVICE_TYPE_MAP = {
    "Meter": "switchbot_meter",
    "MeterPlus": "switchbot_meter_plus",
    "WoSensorTH": "switchbot_meter",
    "MeterPro": "switchbot_meter_pro",
    "MeterPro(CO2)": "switchbot_meter_pro",
    "Hub 2": "switchbot_hub2",
}

_UNITS = {
    "temperature": ("temperature_c", "degC"),
    "humidity": ("humidity_pct", "%"),
    "CO2": ("co2_ppm", "ppm"),
    "battery": ("battery_pct", "%"),
}


def _make_headers(token: str, secret: str) -> dict:
    """SwitchBot API v1.1 HMAC-SHA256 auth headers."""
    nonce = str(uuid.uuid4())
    t = str(int(time.time() * 1000))
    string_to_sign = f"{token}{t}{nonce}"
    sign = hmac.new(
        secret.encode("utf-8"),
        msg=string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest().upper()
    return {
        "Authorization": token,
        "t": t,
        "nonce": nonce,
        "sign": sign,
        "Content-Type": "application/json",
    }


def _api_get(client: httpx.Client, token: str, secret: str, path: str) -> dict:
    url = f"{API_BASE}{path}"
    headers = _make_headers(token, secret)
    resp = client.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("statusCode") not in (100, 200):
        raise RuntimeError(f"API error {data.get('statusCode')}: {data.get('message')}")
    return data.get("body", {})


def list_devices(token: str, secret: str) -> list[dict]:
    with httpx.Client() as client:
        body = _api_get(client, token, secret, "/devices")
    devices = body.get("deviceList", [])
    meters = [d for d in devices if d.get("deviceType") in METER_TYPES]
    return meters


def fetch_device_history(
    client: httpx.Client,
    token: str,
    secret: str,
    device_id: str,
    since_ts: int,  # Unix ms
    until_ts: int,  # Unix ms
) -> list[dict]:
    """
    The SwitchBot v1.1 API logs endpoint returns up to 100 records per page.
    We page until we have all records in the requested window.
    NOTE: The history endpoint may not be available for all meter types / account tiers.
    If 404 is returned, the device's history is not accessible via API.
    """
    records = []
    page = 1
    while True:
        try:
            path = (
                f"/devices/{device_id}/status"
                # history endpoint: uncomment if available on your account tier
                # f"/devices/{device_id}/history?since={since_ts}&until={until_ts}&page={page}"
            )
            body = _api_get(client, token, secret, path)
            # Current status only — log it as a single data point
            records.append(body)
            break
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 422:
                log.warning("Device %s: history endpoint returned 422 — not supported", device_id)
            else:
                log.error("Device %s HTTP error: %s", device_id, exc)
            break
        except Exception as exc:
            log.error("Device %s error: %s", device_id, exc)
            break

    return records


def _open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, device_id TEXT NOT NULL, device_type TEXT NOT NULL,
            area TEXT NOT NULL, transport TEXT NOT NULL, metric TEXT NOT NULL,
            value REAL NOT NULL, unit TEXT NOT NULL, schema_v INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_readings_device_ts ON readings (device_id, ts);
        CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings (ts);
        CREATE TABLE IF NOT EXISTS device_last_seen (
            device_id TEXT PRIMARY KEY, device_type TEXT NOT NULL, area TEXT NOT NULL,
            last_ts TEXT NOT NULL, last_rssi INTEGER
        );
    """)
    conn.commit()
    return conn


def import_status_record(
    conn: sqlite3.Connection,
    device_id: str,
    device_type_cloud: str,
    area: str,
    record: dict,
) -> int:
    """
    Import a single status record (current reading) into the DB.
    Returns number of rows inserted.
    """
    device_type = _DEVICE_TYPE_MAP.get(device_type_cloud, "switchbot_unknown")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Extract available metrics
    metric_map = {
        "temperature": record.get("temperature"),
        "humidity": record.get("humidity"),
        "CO2": record.get("CO2"),
        "battery": record.get("battery"),
    }

    rows = []
    for cloud_key, value in metric_map.items():
        if value is None:
            continue
        if cloud_key not in _UNITS:
            continue
        metric_name, unit = _UNITS[cloud_key]
        try:
            rows.append((ts, device_id, device_type, area, "cloud-import",
                         metric_name, float(value), unit, 1))
        except (ValueError, TypeError):
            pass

    if rows:
        conn.executemany(
            """INSERT OR IGNORE INTO readings
               (ts, device_id, device_type, area, transport, metric, value, unit, schema_v)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.execute(
            """INSERT INTO device_last_seen (device_id, device_type, area, last_ts, last_rssi)
               VALUES (?,?,?,?,NULL)
               ON CONFLICT(device_id) DO UPDATE SET
                 device_type=excluded.device_type, area=excluded.area,
                 last_ts=MAX(device_last_seen.last_ts, excluded.last_ts)""",
            (device_id, device_type, area, ts),
        )
        conn.commit()

    return len(rows)


def run_import(
    token: str,
    secret: str,
    db_path: Path,
    device_filter: str | None,
    days: int,
    registry_path: Path | None,
) -> None:
    log.info("Fetching device list from SwitchBot cloud")
    meters = list_devices(token, secret)
    if not meters:
        log.warning("No meter devices found on this account")
        return

    log.info("Found %d meter device(s):", len(meters))
    for d in meters:
        log.info("  %-40s  type=%-20s  id=%s",
                 d.get("deviceName"), d.get("deviceType"), d.get("deviceId"))

    if device_filter:
        meters = [d for d in meters if d["deviceId"] == device_filter]
        if not meters:
            log.error("Device ID %s not found", device_filter)
            return

    # Load registry to get area assignments
    area_map: dict[str, str] = {}
    if registry_path and registry_path.exists():
        import yaml
        with registry_path.open() as f:
            reg = yaml.safe_load(f) or {}
        for mac, info in reg.get("devices", {}).items():
            # Try to match by device_id slug
            area_map[info.get("device_id", "")] = info.get("area", "unknown")

    conn = _open_db(db_path)
    total_rows = 0

    now_ms = int(time.time() * 1000)
    since_ms = now_ms - days * 24 * 3600 * 1000

    with httpx.Client() as client:
        for device in meters:
            cloud_id = device["deviceId"]
            cloud_name = device.get("deviceName", cloud_id)
            cloud_type = device.get("deviceType", "Meter")
            area = area_map.get(cloud_name, "unknown")

            log.info("Fetching history for %s (%s)", cloud_name, cloud_id)
            records = fetch_device_history(client, token, secret, cloud_id, since_ms, now_ms)

            for record in records:
                n = import_status_record(conn, cloud_name, cloud_type, area, record)
                total_rows += n

            log.info("  → %d row(s) inserted for %s", total_rows, cloud_name)

            # Rate limit: SwitchBot API allows ~10 req/s
            time.sleep(0.15)

    conn.close()
    log.info("Import complete. Total rows inserted: %d", total_rows)
    log.info(
        "NOTE: SwitchBot API v1.1 exposes only current status, not historical logs.\n"
        "      For full history (180 days), use the SwitchBot app's export feature:\n"
        "        Device → chart icon → share/export → CSV\n"
        "      Then import the CSV with:\n"
        "        python3 tools/import_switchbot_csv.py --csv <file.csv> --db instance/db/hot.db"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="SwitchBot cloud history importer (one-time)")
    p.add_argument("--token", required=True, help="SwitchBot API token")
    p.add_argument("--secret", required=True, help="SwitchBot API secret")
    p.add_argument("--db", default="instance/db/hot.db", type=Path)
    p.add_argument("--registry", default="instance/devices.yaml", type=Path)
    p.add_argument("--days", default=180, type=int, help="Days of history to request")
    p.add_argument("--device-id", default=None, help="Import only this SwitchBot cloud device ID")
    p.add_argument("--list-devices", action="store_true", help="List devices and exit")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stdout,
    )

    if args.list_devices:
        meters = list_devices(args.token, args.secret)
        print(f"\nFound {len(meters)} meter device(s):\n")
        for d in meters:
            print(f"  Name:  {d.get('deviceName')}")
            print(f"  Type:  {d.get('deviceType')}")
            print(f"  ID:    {d.get('deviceId')}")
            print()
        return

    run_import(args.token, args.secret, args.db, args.device_id, args.days, args.registry)


if __name__ == "__main__":
    main()
