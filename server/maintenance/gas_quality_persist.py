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
from server.gas_compensation import (air_quality_for, sgp30_air_quality, sgp40_air_quality,  # noqa: E402
                                     sgp41_air_quality, bme680_air_quality, clean_air_baseline)

# family (from device_type substring) → the RAW signal that drives its air-quality + freshness gate.
# The SGP41's gate is voc_index, not nox_index: the NOx pixel is legitimately absent for the first hours
# of a node's life (10 s conditioning, then ~4.75 h of algorithm learning), so gating on it would skip a
# perfectly healthy new node. Order matters for _family's substring match — sgp40 before sgp41 is fine
# because neither string contains the other, but keep them distinct if a future part is named sgp4x.
FAMILY_RAW = {"sgp30": "tvoc", "sgp40": "voc_index", "sgp41": "voc_index", "bme680": "gas_ohm"}


def _family(device_type: str):
    dt = (device_type or "").lower()
    return next((fam for fam in FAMILY_RAW if fam in dt), None)

HOT = REPO / "instance/db/hot.db"
JOIN_TOLERANCE_S = 900       # a gas point without a reference-humidity sample within 15 min is skipped
FRESHNESS_S = 600            # forward sampler: skip a node whose raw gas_ohm is older than this (10 min).
                            # Guards against fabricating a "fresh" derived point from a FROZEN raw input —
                            # e.g. when a node's raw feed dies (migration/outage) the derived series must go
                            # stale too, not keep writing now-stamped values off the last-known gas_ohm.
                            # (raw cadence is ~10s; 10 min tolerates a few missed samples before gating.)


def _epoch(ts: str) -> float:
    return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


def _write(conn, device_id, device_type, area, ts, value):
    conn.execute(
        "INSERT OR IGNORE INTO readings (ts, device_id, device_type, area, transport, metric, value, unit, "
        "schema_v, authoritative) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts, device_id, device_type or "gas", area or "unknown", "derived", "air_quality",
         float(value), "", 1, 1))


def _raw_age_s(conn, device_id, metric, now_ep) -> float | None:
    """Seconds since this node's most recent raw driver sample (None if it has none)."""
    row = conn.execute(
        "SELECT max(ts) FROM readings WHERE device_id=? AND metric=? AND authoritative=1",
        (device_id, metric)).fetchone()
    if not row or not row[0]:
        return None
    return now_ep - _epoch(row[0])


def sample_once(conn) -> int:
    """One forward air_quality point per gas node (SGP30/SGP40/BME680), at 'now', reusing the live-view
    fusion (ADR-0035). Freshness-gated per family: a node is skipped unless its RAW driver signal
    (tvoc / voc_index / gas_ohm) was seen within FRESHNESS_S, so a now-stamped derived point is never
    fabricated from a frozen raw input."""
    now_ep = time.time()
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ep))
    n = 0
    for d in build_sensor_list(conn, now_ep):
        fam = _family(d.get("device_type"))
        if fam is None or d["metrics"].get("air_quality") is None:
            continue
        raw = FAMILY_RAW[fam]
        age = _raw_age_s(conn, d["device_id"], raw, now_ep)
        if age is None or age > FRESHNESS_S:
            print(f"  {d['device_id']}: raw {raw} stale ({'none' if age is None else f'{age:.0f}s'} "
                  f"> {FRESHNESS_S}s) — skipping (not fabricating a fresh air_quality)")
            continue
        _write(conn, d["device_id"], d.get("device_type"), d.get("area"), iso, d["metrics"]["air_quality"])
        n += 1
    conn.commit()
    return n


def _backfill_bme680(conn, gid, dtype, ref, area) -> int:
    """BME680: reconstruct air_quality over gas_ohm history, joining the reference sensor's ambient RH."""
    if not ref:
        print(f"  {gid}: no reference sensor resolved — skipping backfill")
        return 0
    gas = conn.execute("SELECT ts, value FROM readings WHERE device_id=? AND metric='gas_ohm' "
                       "AND authoritative=1 ORDER BY ts", (gid,)).fetchall()
    hum = conn.execute("SELECT ts, value FROM readings WHERE device_id=? AND metric='humidity_pct' "
                       "AND authoritative=1 ORDER BY ts", (ref,)).fetchall()
    if not gas or not hum:
        print(f"  {gid}: no gas/humidity history — skipping")
        return 0
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
            continue                                         # no fresh ambient humidity near this point
        aq = bme680_air_quality(g, h_val[j], baseline)["air_quality"]
        if aq is not None:
            _write(conn, gid, dtype, area, ts, aq)
            n += 1
    print(f"  {gid}: backfilled {n}/{len(gas)} air_quality points (ref={ref}, baseline={baseline:.0f}Ω)")
    return n


def _backfill_simple(conn, gid, dtype, area, raw_metric, fn) -> int:
    """SGP30/SGP40: each raw reading maps straight through its transfer function (no reference join)."""
    rows = conn.execute("SELECT ts, value FROM readings WHERE device_id=? AND metric=? AND authoritative=1 "
                        "ORDER BY ts", (gid, raw_metric)).fetchall()
    n = 0
    for ts, v in rows:
        aq = fn(v)["air_quality"]
        if aq is not None:
            _write(conn, gid, dtype, area, ts, aq)
            n += 1
    print(f"  {gid}: backfilled {n}/{len(rows)} air_quality points ({raw_metric})")
    return n


def _backfill_sgp41(conn, gid, dtype, area) -> int:
    """SGP41: needs BOTH pixels, so it pairs voc_index with nox_index before scoring.

    No time-tolerance join is needed (unlike the BME680, which borrows humidity from a DIFFERENT device):
    both metrics are published in the same MQTT payload by the same node, so they land on an identical
    timestamp. Points with no NOx partner still score — sgp41_air_quality bands them on VOC alone, which
    is exactly right for the node's first hours before the NOx algorithm has learned.
    """
    voc = conn.execute("SELECT ts, value FROM readings WHERE device_id=? AND metric='voc_index' "
                       "AND authoritative=1 ORDER BY ts", (gid,)).fetchall()
    nox = dict(conn.execute("SELECT ts, value FROM readings WHERE device_id=? AND metric='nox_index' "
                            "AND authoritative=1", (gid,)).fetchall())
    n = 0
    for ts, v in voc:
        aq = sgp41_air_quality(v, nox.get(ts))["air_quality"]
        if aq is not None:
            _write(conn, gid, dtype, area, ts, aq)
            n += 1
    print(f"  {gid}: backfilled {n}/{len(voc)} air_quality points (voc_index + {len(nox)} nox_index)")
    return n


def backfill(conn) -> int:
    """Reconstruct air_quality over the full stored history of every gas node (all families)."""
    total = 0
    for d in build_sensor_list(conn, time.time()):
        fam = _family(d.get("device_type"))
        if fam is None:
            continue
        gid, dtype, area = d["device_id"], d.get("device_type"), d.get("area")
        if fam == "bme680":
            total += _backfill_bme680(conn, gid, dtype, d.get("ambient_ref"), area)
        elif fam == "sgp30":
            total += _backfill_simple(conn, gid, dtype, area, "tvoc", lambda v: sgp30_air_quality(v))
        elif fam == "sgp40":
            total += _backfill_simple(conn, gid, dtype, area, "voc_index", lambda v: sgp40_air_quality(v))
        elif fam == "sgp41":
            total += _backfill_sgp41(conn, gid, dtype, area)
        conn.commit()
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
