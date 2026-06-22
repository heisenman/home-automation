#!/usr/bin/env python3
"""
Import an Aranet app CSV export and ingest it idempotently (publishes canonical state → writer).

The app export differs from the device's native units and from the SwitchBot exports:
  - Time is day-first 12-hour local:  "23/03/2026 7:01:38 PM"
  - Radon is **pCi/L** (we store Bq/m³ = pCi/L × 37; verified exact vs the GATT pull)
  - Temperature is **°F** (→ °C)
Columns (fixed order): Time, Radon concentration(pCi/L), Temperature(°F), Relative humidity(%),
Atmospheric pressure(hPa).

Use --end to import only rows BEFORE a cutoff (e.g. the start of the GATT-pulled data) so the overlap
isn't doubled — the writer dedups on (device_id, ts, metric) but the CSV/GATT timestamps differ by a
few minutes, so a cutoff is the clean way to extend coverage backwards.

  python3 tools/import_aranet_csv.py --csv "Aranet ..._all.csv" --device-id aranet_radon \
      --area crawlspace --broker 192.168.0.245 --end 2026-05-23T02:00:00Z
"""
import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt

PCI_TO_BQ = 37.0


def parse_rows(path: Path, tz: ZoneInfo):
    out = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.reader(fh):
            if not r or r[0].lower().startswith("time"):
                continue
            try:
                dt = datetime.strptime(r[0].strip(), "%d/%m/%Y %I:%M:%S %p").replace(tzinfo=tz)
                ts = dt.astimezone(timezone.utc)
                m = {}
                if r[1].strip():
                    m["radon_bqm3"] = int(round(float(r[1]) * PCI_TO_BQ))
                if r[2].strip():
                    m["temperature_c"] = round((float(r[2]) - 32) * 5 / 9, 2)
                if len(r) > 3 and r[3].strip():
                    m["humidity_pct"] = round(float(r[3]), 1)
                if len(r) > 4 and r[4].strip():
                    m["pressure_hpa"] = round(float(r[4]), 1)
                if m:
                    out.append((ts, m))
            except (ValueError, IndexError):
                continue
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Import an Aranet app CSV export (pCi/L, °F, DD/MM 12h)")
    p.add_argument("--csv", required=True, type=Path)
    p.add_argument("--device-id", default="aranet_radon")
    p.add_argument("--area", default="crawlspace")
    p.add_argument("--device-type", default="aranet_radon_plus")
    p.add_argument("--tz", default="America/Los_Angeles")
    p.add_argument("--end", help="only import rows with UTC ts < this ISO (e.g. the GATT-data start)")
    p.add_argument("--broker", default="localhost")
    p.add_argument("--broker-port", type=int, default=1883)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    rows = parse_rows(a.csv, ZoneInfo(a.tz))
    if not rows:
        sys.exit("no rows parsed — check the CSV format")
    end = None
    if a.end:
        end = datetime.fromisoformat(a.end.replace("Z", "+00:00"))
        rows = [r for r in rows if r[0] < end]
    print(f"parsed; importing {len(rows)} rows "
          f"({rows[0][0].isoformat()} .. {rows[-1][0].isoformat()})"
          + (f"  [< {a.end}]" if a.end else ""))
    if a.dry_run:
        print("  [dry-run] sample:", rows[len(rows)//2])
        return

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.connect(a.broker, a.broker_port, 30); c.loop_start()
    topic = f"home/{a.area}/{a.device_id}/state"
    for i, (ts, m) in enumerate(rows):
        msg = {"schema": 1, "device_id": a.device_id, "device_type": a.device_type, "area": a.area,
               "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "transport": "aranet-history", "metrics": m,
               "meta": {"source": "app-csv"}}
        c.publish(topic, json.dumps(msg), qos=1, retain=False)
        if i % 200 == 0:
            time.sleep(0.2)
    time.sleep(1.0)
    c.loop_stop(); c.disconnect()
    print(f"  published {len(rows)} rows (transport=aranet-history); writer dedups by (device_id,ts,metric)")


if __name__ == "__main__":
    main()
