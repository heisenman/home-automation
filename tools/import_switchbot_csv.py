"""
SwitchBot app CSV history importer.

The SwitchBot app exports CSV files with temperature/humidity history.
This tool reads those exports and loads them into the SQLite hot tier.

How to export from the SwitchBot app:
  1. Open the device in the SwitchBot app
  2. Tap the chart / history icon
  3. Tap the share/export icon (top right)
  4. Select "Export as CSV" or similar
  5. Transfer the CSV file to this machine (AirDrop, email, etc.)

Expected CSV formats (the app uses different headers by region/firmware):
  Format A (most common):
    Date,Temp(°C),Humidity(%),Dewpoint(°C)
    2026-01-15 14:30,21.5,48,10.3

  Format B (some firmware):
    Timestamp,Temperature (°C),Humidity (%),Battery (%)
    2026-01-15T14:30:00,21.5,48,92

  Format C (imperial):
    Date,Temp(°F),Humidity(%),Dewpoint(°F)
    2026-01-15 14:30,70.7,48,50.5

The importer auto-detects format and converts °F → °C.

Usage:
  # Single device CSV
  python3 tools/import_switchbot_csv.py \
      --csv ~/Downloads/meter_living_room.csv \
      --device-id meter_living_room \
      --area living_room \
      --device-type switchbot_meter_pro \
      --db instance/db/hot.db

  # Batch: all CSVs in a directory (filenames used as device_id slugs)
  python3 tools/import_switchbot_csv.py \
      --csv-dir ~/Downloads/switchbot_export/ \
      --db instance/db/hot.db

  # Dry run: show what would be imported
  python3 tools/import_switchbot_csv.py --csv file.csv --dry-run
"""

import argparse
import csv
import logging
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("ha.import.csv")

_UNITS = {
    "temperature_c": "degC",
    "humidity_pct": "%",
    "battery_pct": "%",
    "co2_ppm": "ppm",
    "dewpoint_c": "degC",
}


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
        CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_unique ON readings (device_id, ts, metric);
        CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings (ts);
        CREATE TABLE IF NOT EXISTS device_last_seen (
            device_id TEXT PRIMARY KEY, device_type TEXT NOT NULL, area TEXT NOT NULL,
            last_ts TEXT NOT NULL, last_rssi INTEGER
        );
    """)
    conn.commit()
    return conn


def _parse_ts(raw: str) -> str | None:
    """Try multiple date formats, return ISO 8601 UTC string or None."""
    formats = [
        "%Y-%m-%d %H:%M",          # 2026-01-15 14:30
        "%Y-%m-%dT%H:%M:%S",       # 2026-01-15T14:30:00
        "%Y-%m-%dT%H:%M",          # 2026-01-15T14:30
        "%Y/%m/%d %H:%M",          # 2026/01/15 14:30
        "%m/%d/%Y %H:%M",          # 01/15/2026 14:30
        "%d/%m/%Y %H:%M",          # 15/01/2026 14:30
        "%Y-%m-%d %H:%M:%S",       # 2026-01-15 14:30:00
    ]
    raw = raw.strip()
    for fmt in formats:
        try:
            # Treat as local time and convert to UTC marker (device records local time)
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return None


def _fahrenheit_to_celsius(f: float) -> float:
    return round((f - 32) * 5 / 9, 2)


def _detect_columns(headers: list[str]) -> dict[str, str]:
    """
    Return a mapping from our metric names to CSV column names.
    Case-insensitive, tolerant of unit suffixes.
    """
    mapping: dict[str, str] = {}
    for col in headers:
        lower = col.lower().strip()
        if re.search(r"date|time|stamp", lower):
            mapping["ts"] = col
        elif re.search(r"temp.*f\b|\(°f\)|\(f\)", lower):
            mapping["temperature_f"] = col
        elif re.search(r"temp", lower):
            mapping["temperature_c"] = col
        elif re.search(r"humid", lower):
            mapping["humidity_pct"] = col
        elif re.search(r"batter", lower):
            mapping["battery_pct"] = col
        elif re.search(r"co2|co₂", lower):
            mapping["co2_ppm"] = col
        elif re.search(r"dew", lower):
            mapping["dewpoint_c"] = col
    return mapping


def import_csv(
    csv_path: Path,
    device_id: str,
    device_type: str,
    area: str,
    conn: sqlite3.Connection,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Returns (rows_inserted, rows_skipped)."""
    inserted = 0
    skipped = 0
    batch: list[tuple] = []

    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            log.error("CSV has no headers: %s", csv_path)
            return 0, 0

        col_map = _detect_columns(list(reader.fieldnames))
        log.info("CSV columns detected: %s → %s", reader.fieldnames, col_map)

        if "ts" not in col_map:
            log.error("Cannot find timestamp column in %s", csv_path)
            return 0, 0

        for row in reader:
            raw_ts = row.get(col_map["ts"], "").strip()
            ts = _parse_ts(raw_ts)
            if not ts:
                log.debug("Skipping unparseable timestamp: %s", raw_ts)
                skipped += 1
                continue

            for metric_key, col_name in col_map.items():
                if metric_key in ("ts",):
                    continue
                raw_val = row.get(col_name, "").strip()
                if not raw_val:
                    continue
                try:
                    value = float(raw_val)
                except ValueError:
                    continue

                # Convert °F to °C
                if metric_key == "temperature_f":
                    metric_key = "temperature_c"
                    value = _fahrenheit_to_celsius(value)

                unit = _UNITS.get(metric_key, "")
                batch.append((ts, device_id, device_type, area, "csv-import",
                              metric_key, value, unit, 1))

    if dry_run:
        log.info("[DRY RUN] Would insert %d rows for %s", len(batch), device_id)
        return len(batch), skipped

    if batch:
        conn.executemany(
            """INSERT OR IGNORE INTO readings
               (ts, device_id, device_type, area, transport, metric, value, unit, schema_v)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            batch,
        )
        # Update device_last_seen
        if batch:
            last_ts = max(r[0] for r in batch)
            conn.execute(
                """INSERT INTO device_last_seen (device_id, device_type, area, last_ts, last_rssi)
                   VALUES (?,?,?,?,NULL)
                   ON CONFLICT(device_id) DO UPDATE SET
                     device_type=excluded.device_type, area=excluded.area,
                     last_ts=MAX(device_last_seen.last_ts, excluded.last_ts)""",
                (device_id, device_type, area, last_ts),
            )
        conn.commit()
        inserted = len(batch)

    return inserted, skipped


def slug(name: str) -> str:
    """Convert filename to a device_id slug."""
    s = Path(name).stem.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def main() -> None:
    p = argparse.ArgumentParser(description="Import SwitchBot CSV history into SQLite")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--csv", type=Path, help="Single CSV file")
    g.add_argument("--csv-dir", type=Path, help="Directory of CSV files")
    p.add_argument("--db", default="instance/db/hot.db", type=Path)
    p.add_argument("--device-id", default=None, help="device_id slug (--csv mode only)")
    p.add_argument("--device-type", default="switchbot_meter_pro")
    p.add_argument("--area", default="unknown")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stdout,
    )

    conn = None if args.dry_run else _open_db(args.db)

    if args.csv:
        device_id = args.device_id or slug(args.csv.name)
        log.info("Importing %s → device_id=%s area=%s", args.csv, device_id, args.area)
        inserted, skipped = import_csv(
            args.csv, device_id, args.device_type, args.area, conn, args.dry_run
        )
        log.info("Done: %d inserted, %d skipped", inserted, skipped)
    else:
        csv_files = sorted(args.csv_dir.glob("*.csv"))
        log.info("Found %d CSV files in %s", len(csv_files), args.csv_dir)
        total_inserted = 0
        for f in csv_files:
            device_id = slug(f.name)
            log.info("Importing %s → device_id=%s", f.name, device_id)
            inserted, skipped = import_csv(
                f, device_id, args.device_type, args.area, conn, args.dry_run
            )
            log.info("  %d inserted, %d skipped", inserted, skipped)
            total_inserted += inserted
        log.info("Total inserted: %d", total_inserted)

    if conn:
        conn.close()


if __name__ == "__main__":
    main()
