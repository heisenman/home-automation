"""
Direct SwitchBot CSV importer — stdlib only, runs without the venv.
One-shot use; output goes to instance/db/hot.db.
"""
import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB = Path("instance/db/hot.db")
SRC = Path("/home/visko/Desktop/Profile/switchbot")

# filename → (device_id, area, device_type)
DEVICE_MAP = {
    "attic s wall_data.csv":          ("meter_attic_south_wall",  "attic",           "switchbot_meter"),
    "Meter Pro living room_data.csv":  ("meter_pro_living_room",   "living_room",     "switchbot_meter_pro"),
    "MPro COffice_data.csv":           ("meter_pro_c_office",      "c_office",        "switchbot_meter_pro"),
    "MPro MBed_data.csv":              ("meter_pro_master_bed",    "master_bedroom",  "switchbot_meter_pro"),
    "OMeter CBed_data.csv":            ("meter_c_bed",             "c_bedroom",       "switchbot_meter"),
    "OMeter HBath_data.csv":           ("meter_h_bath",            "h_bathroom",      "switchbot_meter"),
    "OMeter HBed_data.csv":            ("meter_h_bed",             "h_bedroom",       "switchbot_meter"),
    "OMeter Kitchen_data.csv":         ("meter_kitchen",            "kitchen",         "switchbot_meter"),
    "OMeter LvngRm_data.csv":          ("meter_living_room",       "living_room",     "switchbot_meter"),
    "OMeter MBath_data.csv":           ("meter_master_bath",       "master_bathroom", "switchbot_meter"),
}

# Column name fragments → (metric_name, unit, fahrenheit?)
COL_RULES = [
    ("Temperature_Fahrenheit", "temperature_c", "degC",  True),
    ("Relative_Humidity",      "humidity_pct",  "%",     False),
    ("DPT",                    "dewpoint_c",    "degC",  True),
    ("VPD",                    "vpd_kpa",       "kPa",   False),
    ("Abs Humidity",           "abs_humidity_gm3", "g/m3", False),
]

DATE_FMTS = [
    "%b %d, %Y %I:%M %p",   # Jan 7, 2026 6:05 PM
    "%b %d, %Y %I:%M:%S %p",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
]

def parse_ts(raw: str) -> str:
    raw = raw.strip().strip('"')
    for fmt in DATE_FMTS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass
    return raw  # leave as-is if unparseable; will surface as bad data

def f_to_c(f: float) -> float:
    return round((f - 32) * 5 / 9, 2)

def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-131072")  # 128 MB cache
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, device_id TEXT NOT NULL, device_type TEXT NOT NULL,
            area TEXT NOT NULL, transport TEXT NOT NULL, metric TEXT NOT NULL,
            value REAL NOT NULL, unit TEXT NOT NULL, schema_v INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_readings_device_ts ON readings (device_id, ts);
        CREATE INDEX IF NOT EXISTS idx_readings_ts        ON readings (ts);
        CREATE INDEX IF NOT EXISTS idx_readings_metric    ON readings (metric, device_id);
        CREATE TABLE IF NOT EXISTS device_last_seen (
            device_id TEXT PRIMARY KEY, device_type TEXT NOT NULL, area TEXT NOT NULL,
            last_ts TEXT NOT NULL, last_rssi INTEGER
        );
    """)
    conn.commit()
    return conn

def import_file(conn, csv_path: Path, device_id: str, area: str, device_type: str) -> int:
    batch = []
    BATCH_SIZE = 10_000

    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        # Match columns to rules
        active_rules = []
        for (frag, metric, unit, is_f) in COL_RULES:
            col = next((h for h in headers if frag in h), None)
            if col:
                active_rules.append((col, metric, unit, is_f))

        # Find timestamp column
        ts_col = next((h for h in headers if h in ("Date", "Timestamp")), headers[0] if headers else None)

        total = 0
        last_ts = ""

        for row in reader:
            raw_ts = row.get(ts_col, "").strip().strip('"')
            if not raw_ts:
                continue
            ts = parse_ts(raw_ts)
            if ts > last_ts:
                last_ts = ts

            for (col, metric, unit, is_f) in active_rules:
                raw = row.get(col, "").strip()
                if not raw:
                    continue
                try:
                    value = float(raw)
                except ValueError:
                    continue
                if is_f:
                    value = f_to_c(value)
                batch.append((ts, device_id, device_type, area, "csv-import", metric, value, unit, 1))

            if len(batch) >= BATCH_SIZE:
                conn.executemany(
                    "INSERT OR IGNORE INTO readings (ts,device_id,device_type,area,transport,metric,value,unit,schema_v) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", batch
                )
                conn.commit()
                total += len(batch)
                batch.clear()
                print(f"  {total:>8,} rows…", end="\r", flush=True)

        if batch:
            conn.executemany(
                "INSERT OR IGNORE INTO readings (ts,device_id,device_type,area,transport,metric,value,unit,schema_v) "
                "VALUES (?,?,?,?,?,?,?,?,?)", batch
            )
            conn.commit()
            total += len(batch)

        if last_ts:
            conn.execute(
                "INSERT INTO device_last_seen (device_id,device_type,area,last_ts,last_rssi) "
                "VALUES (?,?,?,?,NULL) ON CONFLICT(device_id) DO UPDATE SET "
                "device_type=excluded.device_type, area=excluded.area, "
                "last_ts=MAX(device_last_seen.last_ts, excluded.last_ts)",
                (device_id, device_type, area, last_ts)
            )
            conn.commit()

    return total

def main():
    conn = open_db(DB)
    grand_total = 0

    for fname, (device_id, area, device_type) in DEVICE_MAP.items():
        path = SRC / fname
        if not path.exists():
            print(f"MISSING: {fname}")
            continue
        print(f"\n→ {fname}")
        print(f"   device_id={device_id}  area={area}")
        n = import_file(conn, path, device_id, area, device_type)
        grand_total += n
        print(f"   {n:,} rows inserted")

    conn.close()
    print(f"\nDone. Grand total: {grand_total:,} rows in {DB}")

if __name__ == "__main__":
    main()
