#!/usr/bin/env python3
"""gas_quality_persist.py — persist the derived `air_quality` metric so it has a stored time-series
(graphs + history), per the data-storage-is-primary directive. `air_quality` is otherwise computed only in
the read path (the viewmodel fusion), so it never lands on disk. Two modes:

  --once      Compute air_quality NOW for every BME680 gas node and write one reading each. This is the
              FORWARD sampler — run on a timer (systemd ha-gas-quality-sampler.timer).
  --backfill  One-time: reconstruct air_quality across the WHOLE stored gas_ohm history — joining the
              reference sensor's humidity at each timestamp — so the graph shows the sensor's full life.

Both reuse the viewmodel's fusion (auto-picked reference + server/gas_compensation), so a stored point is
identical to what the live view shows. Writes are INSERT OR IGNORE on (device_id, ts, metric) → idempotent
and safe to re-run; the existing compactor/rollup then downsamples air_quality like any other metric.
"""
import argparse
import bisect
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from server.api.viewmodel import build_sensor_list          # noqa: E402  (reuses the fusion + auto-reference)
from server.gas_compensation import air_quality_index, clean_air_baseline  # noqa: E402

HOT = REPO / "instance/db/hot.db"
JOIN_TOLERANCE_S = 900       # a gas point without a reference-humidity sample within 15 min is skipped


def _epoch(ts: str) -> float:
    return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


def _write(conn, device_id, area, ts, value):
    conn.execute(
        "INSERT OR IGNORE INTO readings (ts, device_id, device_type, area, transport, metric, value, unit, "
        "schema_v, authoritative) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts, device_id, "bme680_gas", area or "unknown", "derived", "air_quality", float(value), "", 1, 1))


def sample_once(conn) -> int:
    """One forward point per gas node, at 'now', reusing the live-view fusion."""
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    n = 0
    for d in build_sensor_list(conn, time.time()):
        if d.get("device_type") == "bme680_gas" and d["metrics"].get("air_quality") is not None:
            _write(conn, d["device_id"], d.get("area"), iso, d["metrics"]["air_quality"])
            n += 1
    conn.commit()
    return n


def backfill(conn) -> int:
    """Reconstruct air_quality for the full stored gas_ohm history of each gas node."""
    total = 0
    for d in build_sensor_list(conn, time.time()):
        if d.get("device_type") != "bme680_gas":
            continue
        gid, ref, area = d["device_id"], d.get("ambient_ref"), d.get("area")
        if not ref:
            print(f"  {gid}: no reference sensor resolved — skipping backfill")
            continue
        gas = conn.execute("SELECT ts, value FROM readings WHERE device_id=? AND metric='gas_ohm' "
                           "AND authoritative=1 ORDER BY ts", (gid,)).fetchall()
        hum = conn.execute("SELECT ts, value FROM readings WHERE device_id=? AND metric='humidity_pct' "
                           "AND authoritative=1 ORDER BY ts", (ref,)).fetchall()
        if not gas or not hum:
            print(f"  {gid}: no gas/humidity history — skipping")
            continue
        baseline = clean_air_baseline([v for _, v in gas]) or 1.0
        h_ep = [_epoch(t) for t, _ in hum]
        h_val = [v for _, v in hum]
        n = 0
        for ts, g in gas:
            ge = _epoch(ts)
            i = bisect.bisect_left(h_ep, ge)
            cand = [j for j in (i - 1, i) if 0 <= j < len(h_ep)]
            if not cand:
                continue
            j = min(cand, key=lambda k: abs(h_ep[k] - ge))
            if abs(h_ep[j] - ge) > JOIN_TOLERANCE_S:
                continue                                     # no fresh ambient humidity near this point
            _write(conn, gid, area, ts, air_quality_index(g, h_val[j], baseline)["air_quality"])
            n += 1
        conn.commit()
        print(f"  {gid}: backfilled {n}/{len(gas)} air_quality points (ref={ref}, baseline={baseline:.0f}Ω)")
        total += n
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--once", action="store_true", help="forward sampler: one air_quality point per gas node now")
    g.add_argument("--backfill", action="store_true", help="one-time: reconstruct air_quality over all history")
    ap.add_argument("--db", default=str(HOT))
    a = ap.parse_args()
    conn = sqlite3.connect(a.db)
    try:
        n = backfill(conn) if a.backfill else sample_once(conn)
        print(f"{'backfilled' if a.backfill else 'wrote'} {n} air_quality reading(s)")
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
