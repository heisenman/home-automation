#!/usr/bin/env python3
"""recompute_air_quality.py — recompute the DERIVED `air_quality` series for gas devices across BOTH stores
(hot.db + the monthly Parquet archive) using the CURRENT ADR-0035 transfer functions, replacing any
stale/old-formula values. `air_quality` is derived and fully reproducible from the preserved raw signal, so
this touches no raw data. One clean-air baseline per device is computed over the device's COMBINED
(parquet+hot) gas_ohm history, so the whole series is on one consistent baseline (no hot/parquet seam).

Safety: each affected Parquet file is rewritten atomically — original backed up to `<file>.bak-<stamp>`,
the complement (every row that is NOT this recompute's target) is row-count-verified identical, then the new
file is swapped in and the manifest (sha256+size+rows) updated. hot.db writes are a delete+insert in one txn.

Usage: recompute_air_quality.py --device gas_hbed:meter_h_bed_closet --device gas_h_office:meter_pro_h_bed
       [--parquet PATH] [--hot PATH] [--stamp YYYYmmddHHMMSS] [--dry-run]
`--device gas_id:ref_id` pairs a BME680 gas node with its ambient-humidity reference. (SGP30/SGP40 take no
ref; pass `gas_c_bed:` — empty ref.) `--stamp` names the backup deterministically (no clock in unit tests).
"""
import argparse
import hashlib
import sqlite3
import sys
import time
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from server.gas_compensation import (air_quality_for, clean_air_baseline)  # noqa: E402

HOT = REPO / "instance/db/hot.db"
PARQUET = REPO / "instance/db/parquet/year=2026/month=07/2026-07.parquet"
MANIFEST = REPO / "instance/db/parquet/manifest.json"
JOIN_TOLERANCE_S = 900


def _epoch(ts: str) -> float:
    return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


def _series(dpath, hot, device_id, metric):
    """(ts, value) for device_id+metric across parquet + hot.db, sorted by ts, deduped on ts."""
    rows = {}
    for ts, v in duckdb.query(
            f"SELECT ts, value FROM read_parquet('{dpath}') WHERE device_id=? AND metric=?",
            params=[device_id, metric]).fetchall():
        rows[ts] = v
    for ts, v in hot.execute("SELECT ts, value FROM readings WHERE device_id=? AND metric=? AND authoritative=1",
                             (device_id, metric)).fetchall():
        rows[ts] = v
    return sorted(rows.items())


def _device_type(dpath, hot, device_id):
    r = duckdb.query(f"SELECT device_type FROM read_parquet('{dpath}') WHERE device_id=? AND device_type IS NOT NULL LIMIT 1",
                     params=[device_id]).fetchone()
    if r:
        return r[0]
    r = hot.execute("SELECT device_type FROM readings WHERE device_id=? AND device_type IS NOT NULL LIMIT 1",
                    (device_id,)).fetchone()
    return r[0] if r else "bme680_gas"


def _area(dpath, hot, device_id):
    r = hot.execute("SELECT area FROM readings WHERE device_id=? AND area IS NOT NULL LIMIT 1", (device_id,)).fetchone()
    return (r[0] if r else None) or "unknown"


def _raw_metric(device_type):
    """The driving raw signal per family. NB `sgp41` must be matched explicitly — it does not contain the
    substring "sgp40", so without this it would fall through to gas_ohm, find no such series, and recompute
    NOTHING while reporting success."""
    dt = (device_type or "").lower()
    if "sgp30" in dt:
        return "tvoc"
    if "sgp40" in dt or "sgp41" in dt:
        return "voc_index"
    return "gas_ohm"


def recompute_device(dpath, hot, gas_id, ref_id):
    """Return (device_type, area, [(ts, air_quality_value)]) recomputed over the full combined history."""
    dtype = _device_type(dpath, hot, gas_id)
    area = _area(dpath, hot, gas_id)
    raw = _raw_metric(dtype)
    gas = _series(dpath, hot, gas_id, raw)
    if not gas:
        return dtype, area, []
    baseline = clean_air_baseline([v for _, v in gas]) if "bme680" in dtype.lower() else None
    # SGP41's second pixel. Same node, same payload → exact timestamp match, no tolerance join needed.
    nox = dict(_series(dpath, hot, gas_id, "nox_index")) if "sgp41" in dtype.lower() else {}
    hum = _series(dpath, hot, ref_id, "humidity_pct") if ref_id else []
    h_ep = [_epoch(t) for t, _ in hum]
    h_val = [v for _, v in hum]
    import bisect
    out = []
    for ts, v in gas:
        rh = None
        if h_ep:
            i = bisect.bisect_left(h_ep, _epoch(ts))
            cand = [j for j in (i - 1, i) if 0 <= j < len(h_ep)]
            if cand:
                j = min(cand, key=lambda k: abs(h_ep[k] - _epoch(ts)))
                if abs(h_ep[j] - _epoch(ts)) <= JOIN_TOLERANCE_S:
                    rh = h_val[j]
        metrics = {raw: v}
        if ts in nox:
            metrics["nox_index"] = nox[ts]
        rep = air_quality_for(dtype, metrics, ambient_rh_pct=rh, gas_baseline_ohm=baseline)
        if rep and rep.get("air_quality") is not None:
            out.append((ts, rep["air_quality"]))
    return dtype, area, out


def _manifest_update(parquet_path, stamp):
    import json
    rel = str(parquet_path.relative_to(MANIFEST.parent))
    data = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {"files": {}}
    files = data.get("files", {})
    raw = parquet_path.read_bytes()
    files[rel] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                  "rows": duckdb.query(f"SELECT count(*) FROM read_parquet('{parquet_path}')").fetchone()[0],
                  "recomputed_air_quality": stamp}
    data["files"] = files
    MANIFEST.write_text(json.dumps(data, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", action="append", required=True, help="gas_id:ref_id (ref empty for SGP)")
    ap.add_argument("--parquet", default=str(PARQUET))
    ap.add_argument("--hot", default=str(HOT))
    ap.add_argument("--stamp", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    stamp = a.stamp or time.strftime("%Y%m%d%H%M%S", time.gmtime())
    dpath, hpath = Path(a.parquet), Path(a.hot)
    pairs = [(d.split(":", 1) + [""])[:2] for d in a.device]
    hot = sqlite3.connect(hpath)
    hot_min = hot.execute("SELECT min(ts) FROM readings WHERE metric='air_quality'").fetchone()[0] or "9999"

    computed = {}
    for gas_id, ref_id in pairs:
        dtype, area, rows = recompute_device(dpath, hot, gas_id, ref_id or None)
        computed[gas_id] = (dtype, area, rows)
        below = sum(1 for ts, _ in rows if ts < hot_min)
        print(f"  {gas_id}: recomputed {len(rows)} points ({below} → parquet, {len(rows) - below} → hot)")
    if a.dry_run:
        print("dry-run: no writes"); return 0

    ids = [g for g, _ in pairs]
    id_list = ",".join(f"'{i}'" for i in ids)
    # 1) PARQUET rewrite (rows with ts < hot_min). Build new air_quality rows via a temp parquet, then swap.
    tmp_new_rows = dpath.with_suffix(".aqrows.parquet")
    import pyarrow as pa, pyarrow.parquet as pq
    recs = {"ts": [], "device_id": [], "device_type": [], "area": [], "transport": [],
            "metric": [], "value": [], "unit": [], "schema_v": [], "month": [], "year": []}
    for gas_id, (dtype, area, rows) in computed.items():
        for ts, val in rows:
            if ts >= hot_min:
                continue
            recs["ts"].append(ts); recs["device_id"].append(gas_id); recs["device_type"].append(dtype)
            recs["area"].append(area); recs["transport"].append("derived"); recs["metric"].append("air_quality")
            recs["value"].append(float(val)); recs["unit"].append(""); recs["schema_v"].append(1)
            recs["month"].append(dpath.stem.split("-")[1]); recs["year"].append(int(dpath.stem.split("-")[0]))
    pq.write_table(pa.table(recs), tmp_new_rows)

    complement_before = duckdb.query(
        f"SELECT count(*) FROM read_parquet('{dpath}') WHERE NOT (metric='air_quality' AND device_id IN ({id_list}))"
    ).fetchone()[0]
    tmp_out = dpath.with_suffix(".rewrite.parquet")
    duckdb.query(f"""COPY (
        SELECT ts,device_id,device_type,area,transport,metric,value,unit,schema_v,month,year
          FROM read_parquet('{dpath}') WHERE NOT (metric='air_quality' AND device_id IN ({id_list}))
        UNION ALL
        SELECT ts,device_id,device_type,area,transport,metric,value::DOUBLE,unit,
               schema_v::INTEGER,month,year::BIGINT
          FROM read_parquet('{tmp_new_rows}')
    ) TO '{tmp_out}' (FORMAT PARQUET)""")
    complement_after = duckdb.query(
        f"SELECT count(*) FROM read_parquet('{tmp_out}') WHERE NOT (metric='air_quality' AND device_id IN ({id_list}))"
    ).fetchone()[0]
    assert complement_after == complement_before, f"complement changed {complement_before}->{complement_after}!"
    bak = dpath.with_name(dpath.name + f".bak-{stamp}")
    dpath.rename(bak)
    tmp_out.rename(dpath)
    tmp_new_rows.unlink(missing_ok=True)
    _manifest_update(dpath, stamp)
    print(f"  parquet rewritten (backup {bak.name}); complement {complement_before} rows preserved")

    # 2) hot.db (rows with ts >= hot_min) — delete + insert in one txn
    hot.execute(f"DELETE FROM readings WHERE metric='air_quality' AND device_id IN ({id_list})")
    n = 0
    for gas_id, (dtype, area, rows) in computed.items():
        for ts, val in rows:
            if ts < hot_min:
                continue
            hot.execute("INSERT OR IGNORE INTO readings (ts,device_id,device_type,area,transport,metric,value,unit,"
                        "schema_v,authoritative) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (ts, gas_id, dtype, area, "derived", "air_quality", float(val), "", 1, 1))
            n += 1
    hot.commit()
    print(f"  hot.db: {n} air_quality rows rewritten")
    hot.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
