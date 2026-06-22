#!/usr/bin/env python3
"""
Repair a meter whose history got double-imported across tiers.

Symptom: a graph shows a duplicate "band" because the readings API unions hot.db + parquet and BOTH
hold rows for the same timestamps (e.g. a timezone-corrected CSV re-import landed in hot.db for dates
already compacted to parquet, while the stale/time-shifted copy stayed in parquet). The API now dedups
(hot wins) so overlaps are hidden, but parquet-only older dates still serve the WRONG values. This tool
makes ONE correct dataset: purge the device's <transport> rows from BOTH tiers, then re-import a fresh
CSV correctly.

SAFE BY DEFAULT: backs up hot.db + parquet first; --dry-run previews with zero writes. Run on the box
that holds the DB (the dictator). Verify the printed before/after counts.

  # preview (no writes)
  python3 tools/fix_meter_reimport.py --device-id meter_attic_south_wall \
      --csv "$HOME/home_automation/attic s wall_data.csv" --area attic \
      --device-type switchbot_meter_outdoor --dry-run

  # for real (writes, after a backup)
  python3 tools/fix_meter_reimport.py --device-id meter_attic_south_wall \
      --csv "$HOME/home_automation/attic s wall_data.csv" --area attic \
      --device-type switchbot_meter_outdoor
"""
import argparse
import glob
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import import_switchbot_csv as imp  # reuse the proven (tz/°F/humidity-correct) importer


def _hot_count(db: Path, device_id: str, transport: str) -> int:
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT count(*) FROM readings WHERE device_id=? AND transport=?",
                           (device_id, transport)).fetchone()[0]
    finally:
        con.close()


def purge_hot(db: Path, device_id: str, transport: str, dry: bool) -> int:
    n = _hot_count(db, device_id, transport)
    if n and not dry:
        con = sqlite3.connect(str(db))
        con.execute("DELETE FROM readings WHERE device_id=? AND transport=?", (device_id, transport))
        con.commit(); con.close()
    return n


def _sql(s: str) -> str:                      # escape a string for a literal SQL value
    return s.replace("'", "''")


def purge_parquet(pdir: Path, device_id: str, transport: str, dry: bool) -> int:
    files = sorted(glob.glob(str(pdir / "**" / "*.parquet"), recursive=True))
    total = 0
    for f in files:
        con = duckdb.connect()
        try:
            n = con.execute("SELECT count(*) FROM read_parquet(?) WHERE device_id=? AND transport=?",
                            [f, device_id, transport]).fetchone()[0]
            if n:
                print(f"    {f}: {n} rows")
                if not dry:
                    tmp = f + ".tmp"
                    # COPY needs literal paths (no bound params for the TO target) — inline + escape.
                    con.execute(
                        f"COPY (SELECT * FROM read_parquet('{_sql(f)}') "
                        f"WHERE NOT (device_id='{_sql(device_id)}' AND transport='{_sql(transport)}')) "
                        f"TO '{_sql(tmp)}' (FORMAT PARQUET, COMPRESSION ZSTD)")
                    Path(tmp).replace(f)
                total += n
        finally:
            con.close()
    return total


def backup(db: Path, pdir: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = db.parent / f"backup-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    if db.exists():
        shutil.copy2(db, dest / db.name)
    if pdir.exists():
        shutil.copytree(pdir, dest / pdir.name)
    return dest


def main() -> None:
    p = argparse.ArgumentParser(description="Purge a meter's transport rows from both tiers + re-import")
    p.add_argument("--device-id", required=True)
    p.add_argument("--csv", required=True, type=Path)
    p.add_argument("--area", required=True)
    p.add_argument("--device-type", default="switchbot_meter_outdoor")
    p.add_argument("--transport", default="csv-import", help="transport label to purge (default csv-import)")
    p.add_argument("--db", default=Path.home() / "home_automation/instance/db/hot.db", type=Path)
    p.add_argument("--parquet-dir", default=Path.home() / "home_automation/instance/db/parquet", type=Path)
    p.add_argument("--tz", default="America/Los_Angeles")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-backup", action="store_true")
    a = p.parse_args()

    if not a.csv.exists():
        sys.exit(f"CSV not found: {a.csv}")
    mode = "DRY-RUN (no writes)" if a.dry_run else "LIVE"
    print(f"== fix_meter_reimport [{mode}] device={a.device_id} transport={a.transport} ==")

    if not a.dry_run and not a.no_backup:
        dest = backup(a.db, a.parquet_dir)
        print(f"  backup -> {dest}")

    print(f"  hot.db rows to purge: {purge_hot(a.db, a.device_id, a.transport, a.dry_run)}")
    print("  parquet rows to purge (per file):")
    print(f"  parquet total purged: {purge_parquet(a.parquet_dir, a.device_id, a.transport, a.dry_run)}")

    print(f"  re-import {a.csv.name} ...")
    if a.dry_run:
        from zoneinfo import ZoneInfo
        ins, skip = imp.import_csv(a.csv, a.device_id, a.device_type, a.area, None,
                                   dry_run=True, tz=ZoneInfo(a.tz))
        print(f"    would insert {ins} rows ({skip} skipped)")
    else:
        from zoneinfo import ZoneInfo
        conn = imp._open_db(a.db)
        ins, skip = imp.import_csv(a.csv, a.device_id, a.device_type, a.area, conn,
                                   dry_run=False, tz=ZoneInfo(a.tz))
        conn.close()
        print(f"    inserted {ins} rows ({skip} skipped)")
        print(f"  hot.db {a.transport} rows now: {_hot_count(a.db, a.device_id, a.transport)}")
    print("  done. (next compaction moves the corrected hot rows to parquet cleanly)")


if __name__ == "__main__":
    main()
