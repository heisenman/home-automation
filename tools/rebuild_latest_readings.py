#!/usr/bin/env python3
"""rebuild_latest_readings.py — build / verify the materialized latest-reading cache (hot.db).

Board task `sensors-query-unbounded`. See server/storage/latest_cache.py for the design.

The cache is maintained by a SQLite trigger on INSERT, which covers every writer (including
failover/reconcile-history.sh, which inserts via the bash `sqlite3` CLI). What a trigger on INSERT
cannot see is a bulk **UPDATE** of `device_id` — which is exactly what `device_migrate` /
`device_relocate` do when a sensor is renamed or moved. Run this afterwards.

  rebuild_latest_readings.py --verify     # compare cache vs the O(rows) derivation; exit 1 on drift
  rebuild_latest_readings.py --rebuild    # recompute from scratch
  rebuild_latest_readings.py --bench      # time the cached path vs the query it replaces
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from server.storage import latest_cache  # noqa: E402

HOT = REPO / "instance/db/hot.db"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(HOT))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--verify", action="store_true", help="compare against the O(rows) derivation")
    g.add_argument("--rebuild", action="store_true", help="recompute the cache from readings")
    g.add_argument("--bench", action="store_true", help="time cached vs uncached")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    try:
        if a.rebuild:
            n = latest_cache.rebuild(conn)
            print(f"rebuilt latest_readings: {n} device-metric pairs")
            return 0

        if a.verify:
            if not latest_cache.is_usable(conn):
                print("latest_readings is NOT usable (missing table/trigger, or empty) — "
                      "readers are falling back to the slow query. Run --rebuild.")
                return 1
            ndiff, sample = latest_cache.compare_with_source(conn)
            if ndiff == 0:
                n = conn.execute("SELECT count(*) FROM latest_readings").fetchone()[0]
                print(f"OK — cache matches the source derivation exactly ({n} pairs)")
                return 0
            print(f"DRIFT — {ndiff} device-metric pair(s) disagree with the source derivation:")
            for d in sample:
                print(f"  {d['key']}: expected={d['expected']} cached={d['cached']}")
            print("Run --rebuild (and check what UPDATEd device_id without a rebuild).")
            return 1

        # --bench
        rows = conn.execute("SELECT count(*) FROM readings").fetchone()[0]
        t0 = time.perf_counter()
        slow = conn.execute(latest_cache.LATEST_SELECT).fetchall()
        t_slow = time.perf_counter() - t0
        t0 = time.perf_counter()
        fast = conn.execute("SELECT device_id, metric, value, ts FROM latest_readings").fetchall()
        t_fast = time.perf_counter() - t0
        print(f"readings rows      : {rows:,}")
        print(f"uncached GROUP BY  : {t_slow*1000:8.1f} ms  -> {len(slow)} pairs")
        print(f"cached lookup      : {t_fast*1000:8.1f} ms  -> {len(fast)} pairs")
        if t_fast > 0:
            print(f"speedup            : {t_slow/t_fast:8.1f}x")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
