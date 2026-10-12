#!/usr/bin/env python3
"""gas_quality_persist.py — persist the derived `air_quality` metric so it has a stored time-series
(graphs + history), per the data-storage-is-primary directive. `air_quality` is otherwise computed only in
the read path (the viewmodel fusion), so it never lands on disk. Two modes:

  --once      Compute air_quality NOW for every gas node and write one reading each, plus the SHADOW
              `air_quality_v2` (ADR-0035 amendment 2026-10-10; BME680 differs, Sensirion carries v1) and
              `air_quality_v3` (Phase 1: the BME680 through Sensirion's VOC Index algorithm) and
              `air_quality_tc` (Phase 4a: SGP40/41 corrected for a same-room temperature). This is the
              FORWARD sampler — run on a timer (systemd ha-gas-quality-sampler.timer). Also writes the
              SHADOW `air_quality_pm25` for any PM2.5-capable device (Phase 3) and the Phase 2 context
              `aq_local_excess` / `aq_hour_anomaly` per gas node.
  --backfill  One-time: reconstruct air_quality across the WHOLE stored gas_ohm history — joining the
              reference sensor's humidity at each timestamp — so the graph shows the sensor's full life.

Both reuse the viewmodel's fusion (auto-picked reference + server/gas_compensation), so a stored point is
identical to what the live view shows. Writes are INSERT OR IGNORE on (device_id, ts, metric) → idempotent
and safe to re-run; the existing compactor/rollup then downsamples air_quality like any other metric.
"""
import argparse
import bisect
import calendar
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from server.api.viewmodel import build_sensor_list          # noqa: E402  (reuses the fusion + auto-reference)
from server.gas_compensation import (air_quality_for, sgp30_air_quality, sgp40_air_quality,  # noqa: E402
                                     sgp41_air_quality, bme680_air_quality, clean_air_baseline,
                                     bme680_air_quality_v2, bme680_v2_baseline, BME_V2_WINDOW_S,
                                     bme680_air_quality_v3, bme680_voc_index, BME_V3_WINDOW_S,
                                     sgp_air_quality_tc, sgp_tc_voc_index, SGP_TC_WINDOW_S,
                                     pm25_air_quality, PM25_WINDOW_S,
                                     AQ_CTX_WINDOW_S, AQ_OFFSET_MIN_N, AQ_PROFILE_MIN_N, aq_context,
                                     area_worst, house_median)

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
RUNGS = REPO / "instance/db/rungs.db"   # 1-min rollup rung (7 d) — the only store holding the v2 48 h window
JOIN_TOLERANCE_S = 900       # a gas point without a reference-humidity sample within 15 min is skipped
FRESHNESS_S = 600            # forward sampler: skip a node whose raw gas_ohm is older than this (10 min).
                            # Guards against fabricating a "fresh" derived point from a FROZEN raw input —
                            # e.g. when a node's raw feed dies (migration/outage) the derived series must go
                            # stale too, not keep writing now-stamped values off the last-known gas_ohm.
                            # (raw cadence is ~10s; 10 min tolerates a few missed samples before gating.)


def _epoch(ts: str) -> float:
    return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


def _write(conn, device_id, device_type, area, ts, value, metric="air_quality"):
    conn.execute(
        "INSERT OR IGNORE INTO readings (ts, device_id, device_type, area, transport, metric, value, unit, "
        "schema_v, authoritative) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts, device_id, device_type or "gas", area or "unknown", "derived", metric,
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
    devices = build_sensor_list(conn, now_ep)
    now_scores, node_area, node_fam = {}, {}, {}
    for d in devices:
        fam = _family(d.get("device_type"))
        if fam is None:
            continue
        raw = FAMILY_RAW[fam]
        age = _raw_age_s(conn, d["device_id"], raw, now_ep)
        if age is None or age > FRESHNESS_S:
            print(f"  {d['device_id']}: raw {raw} stale ({'none' if age is None else f'{age:.0f}s'} "
                  f"> {FRESHNESS_S}s) — skipping (not fabricating a fresh air_quality)")
            continue
        v1 = d["metrics"].get("air_quality")
        if v1 is not None:
            _write(conn, d["device_id"], d.get("device_type"), d.get("area"), iso, v1)
            n += 1
        # SHADOW v2 (ADR-0035 amendment 2026-10-10): only the BME680 differs in Phase 0; the Sensirion
        # families carry v1 through so air_quality_v2 is a complete series to compare room-to-room.
        v2 = _bme680_v2(conn, d["device_id"], now_ep) if fam == "bme680" else v1
        if v2 is not None:
            _write(conn, d["device_id"], d.get("device_type"), d.get("area"), iso, v2, "air_quality_v2")
            now_scores[d["device_id"]] = v2
            node_area[d["device_id"]] = d.get("area")
            node_fam[d["device_id"]] = (fam, d.get("device_type"))
        # SHADOW v3 (Phase 1, 2026-10-12): BME680 on the SGP's VOC Index scale; Sensirion carries v1 again.
        v3 = _bme680_v3(conn, d["device_id"], now_ep) if fam == "bme680" else v1
        if v3 is not None:
            _write(conn, d["device_id"], d.get("device_type"), d.get("area"), iso, v3, "air_quality_v3")
        # SHADOW Phase 4a (2026-10-12): SGP40/41 voc_raw corrected for a same-room temperature, re-indexed.
        if fam in ("sgp40", "sgp41"):
            src = _temp_source(devices, d.get("area"), d["device_id"])
            tc = _sgp_tc(d["device_id"], src["device_id"], now_ep) if src else None
            if tc is not None:
                _write(conn, d["device_id"], d.get("device_type"), d.get("area"), iso, tc, "air_quality_tc")
    # SHADOW Phase 2: house-wide vs room-local + hour-of-day expectation, per gas node.
    for did, ctx in _aq_contexts(now_scores, node_area, {k: v[0] for k, v in node_fam.items()}, now_ep).items():
        for metric, key in (("aq_local_excess", "local_excess"), ("aq_hour_anomaly", "hour_anomaly")):
            if ctx[key] is not None:
                _write(conn, did, node_fam[did][1], node_area[did], iso, ctx[key], metric)
    # SHADOW Phase 2b: humidity-based "room closed off / accumulating" proxy, per main-floor room meter.
    for did, (area, dtype, coup, excess) in _ventilation(devices, now_ep).items():
        if coup is not None:
            _write(conn, did, dtype, area, iso, round(coup, 3), "vent_coupling")
        if excess is not None:
            _write(conn, did, dtype, area, iso, round(excess, 3), "vent_ah_excess")
    # SHADOW Phase 3: any device with the PM2.5 ability (by metric, not by type — conform by ability).
    for d in devices:
        if d["metrics"].get("pm25_ugm3") is None:
            continue
        age = _raw_age_s(conn, d["device_id"], "pm25_ugm3", now_ep)
        if age is None or age > FRESHNESS_S:
            continue
        pm = _pm25_score(d["device_id"], now_ep)
        if pm is not None:
            _write(conn, d["device_id"], d.get("device_type"), d.get("area"), iso, pm, "air_quality_pm25")
    conn.commit()
    return n


def _house_tz() -> str:
    """The house `timezone` setting (PWA) — the dictator's clock is UTC, so hour-of-day must come from here."""
    from server.control.automation import HOUSE_TZ_DEFAULT, valid_tz
    try:
        from server.control import control_store as store
        cdb = REPO / "instance/db/control.db"
        if cdb.exists():
            cc = sqlite3.connect(f"file:{cdb}?mode=ro", uri=True)
            try:
                tz = store.get_setting(cc, "timezone")
            finally:
                cc.close()
            if valid_tz(tz):
                return tz
    except Exception:
        pass
    return HOUSE_TZ_DEFAULT


def _aq_contexts(now_scores, node_area, node_fam, now_ep, rungs_path=None, tz=None) -> dict:
    """{node: aq_context(...)} from the 1-hour rung over AQ_CTX_WINDOW_S. History metric per node: the
    Sensirion families' v2 IS v1, so their long `air_quality` history serves; the BME680 uses its own v2 history
    (short at first → offset/typical stay None until enough has banked — that's the honest 'unknown')."""
    from datetime import datetime
    from statistics import median
    from zoneinfo import ZoneInfo
    path = Path(rungs_path or RUNGS)
    if not now_scores or not path.exists():
        return {}
    zone = ZoneInfo(tz or _house_tz())
    hist: dict[str, dict[int, float]] = {}
    rc = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for did in now_scores:
            metric = "air_quality_v2" if node_fam.get(did) == "bme680" else "air_quality"
            hist[did] = dict(rc.execute(
                "SELECT bucket_start, vmean FROM rung WHERE res='1hour' AND device_id=? AND metric=? "
                "AND bucket_start >= ? AND vmean IS NOT NULL", (did, metric, int(now_ep - AQ_CTX_WINDOW_S))))
    finally:
        rc.close()
    buckets = sorted({b for h in hist.values() for b in h})
    house_h = {b: house_median(area_worst({d: h.get(b) for d, h in hist.items()}, node_area)) for b in buckets}
    house_now = house_median(area_worst(now_scores, node_area))
    hour_now = datetime.fromtimestamp(now_ep, zone).hour
    out = {}
    for did, score in now_scores.items():
        h = hist.get(did, {})
        ex = [v - house_h[b] for b, v in h.items() if house_h.get(b) is not None]
        same_hour = [v for b, v in h.items() if datetime.fromtimestamp(b, zone).hour == hour_now]
        offset = median(ex) if len(ex) >= AQ_OFFSET_MIN_N else None
        typical = median(same_hour) if len(same_hour) >= AQ_PROFILE_MIN_N else None
        out[did] = aq_context(score, house_now, offset, typical)
    return out


def _room_meters(devices, area_meta) -> dict:
    """{area: meter device} — one reliable T/RH meter per main-floor living area (same preference as the
    ambient-ref resolver: a 'pro' meter, then the freshest). Gas nodes are self-heated → never a meter."""
    from server.room_ventilation import EXCLUDED_TYPES
    best: dict = {}
    for d in devices:
        dt = (d.get("device_type") or "").lower()
        m = d.get("metrics") or {}
        if "gas" in dt or "bme680" in dt or m.get("temperature_c") is None or m.get("humidity_pct") is None:
            continue
        meta = area_meta.get(d.get("area")) or {}
        if meta.get("level") != "main_floor" or meta.get("type") in EXCLUDED_TYPES:
            continue
        cur = best.get(d["area"])
        if cur is None or _meter_rank(d) < _meter_rank(cur):
            best[d["area"]] = d
    return best


def _meter_rank(d):
    """Lower is better: a 'pro' meter first, then the freshest reading."""
    ts = d.get("ts")
    return (0 if "pro" in d["device_id"] else 1, -_epoch(ts) if ts else 0.0)


def _ventilation(devices, now_ep, rungs_path=None, area_meta=None) -> dict:
    """{meter_id: (area, device_type, coupling, excess)} — Phase 2b (server/room_ventilation.py)."""
    from server.api.viewmodel import _area_meta
    from server.room_ventilation import (COUPLING_WINDOW_S, EXCESS_MIN_N, EXCESS_WINDOW_S, STEP_S, ah_series,
                                         coupling, house_series, median)
    path = Path(rungs_path or RUNGS)
    meters = _room_meters(devices, area_meta if area_meta is not None else _area_meta())
    if len(meters) < 4 or not path.exists():                  # a room + ≥ 3 others for the house
        return {}
    since15, sinceH = int(now_ep - COUPLING_WINDOW_S - STEP_S), int(now_ep - EXCESS_WINDOW_S)
    ah15, ahH = {}, {}
    rc = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for area, d in meters.items():
            raw = {"temperature_c": {}, "humidity_pct": {}}
            for b, metric, v in rc.execute(
                    "SELECT bucket_start, metric, vmean FROM rung WHERE res='1min' AND device_id=? "
                    "AND metric IN ('temperature_c','humidity_pct') AND bucket_start >= ? AND vmean IS NOT NULL",
                    (d["device_id"], since15)):
                raw[metric].setdefault(b // STEP_S * STEP_S, []).append(v)
            ah15[area] = ah_series({b: sum(v) / len(v) for b, v in raw["temperature_c"].items()},
                                   {b: sum(v) / len(v) for b, v in raw["humidity_pct"].items()})
            hr = {"temperature_c": {}, "humidity_pct": {}}
            for b, metric, v in rc.execute(
                    "SELECT bucket_start, metric, vmean FROM rung WHERE res='1hour' AND device_id=? "
                    "AND metric IN ('temperature_c','humidity_pct') AND bucket_start >= ? AND vmean IS NOT NULL",
                    (d["device_id"], sinceH)):
                hr[metric][b] = v
            ahH[area] = ah_series(hr["temperature_c"], hr["humidity_pct"])
    finally:
        rc.close()
    out = {}
    for area, d in meters.items():
        h15 = house_series(ah15, area)
        coup = coupling(ah15[area], h15)
        hH = house_series(ahH, area)
        diffs = [v - hH[b] for b, v in ahH[area].items() if b in hH]
        offset = median(diffs) if len(diffs) >= EXCESS_MIN_N else None
        common = sorted(set(ah15[area]) & set(h15))
        excess = (ah15[area][common[-1]] - h15[common[-1]] - offset) if common and offset is not None else None
        out[d["device_id"]] = (area, d.get("device_type"), coup, excess)
    return out


def _pm25_score(did, now_ep, rungs_path=None):
    """1 h mean PM2.5 from the 1-min rung (count-weighted) → EPA-banded score, or None."""
    path = Path(rungs_path or RUNGS)
    if not path.exists():
        return None
    rc = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = rc.execute("SELECT vmean, vcount FROM rung WHERE res='1min' AND device_id=? AND metric='pm25_ugm3' "
                          "AND bucket_start >= ? AND vmean IS NOT NULL", (did, int(now_ep - PM25_WINDOW_S))).fetchall()
    finally:
        rc.close()
    w = sum(c or 1 for _, c in rows)
    mean = sum(v * (c or 1) for v, c in rows) / w if rows else None
    return pm25_air_quality(mean, len(rows))["air_quality"]


def _bme680_v2(conn, gid, now_ep, rungs_path=None):
    """v2 score for one BME680 node now: latest raw payload (gas_ohm + its own die T/RH, same ts) against a
    48 h p95 baseline from the 1-min rung. None (nothing written) when the rung is unavailable or the
    baseline is still forming — the gap is the honest record of 'no v2 yet'."""
    row = conn.execute("SELECT max(ts) FROM readings WHERE device_id=? AND metric='gas_ohm' AND authoritative=1",
                       (gid,)).fetchone()
    if not row or not row[0]:
        return None
    m = dict(conn.execute("SELECT metric, value FROM readings WHERE device_id=? AND ts=? AND authoritative=1 "
                          "AND metric IN ('gas_ohm','temperature_c','humidity_pct','gas_valid')",
                          (gid, row[0])).fetchall())
    path = Path(rungs_path or RUNGS)
    if not path.exists():
        return None
    rc = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        by_b: dict[int, dict] = {}
        for b, metric, v in rc.execute(
                "SELECT bucket_start, metric, vmean FROM rung WHERE res='1min' AND device_id=? "
                "AND metric IN ('gas_ohm','temperature_c','humidity_pct') AND bucket_start >= ?",
                (gid, int(now_ep - BME_V2_WINDOW_S))):
            by_b.setdefault(b, {})[metric] = v
    finally:
        rc.close()
    samples = [(x.get("gas_ohm"), x.get("temperature_c"), x.get("humidity_pct")) for x in by_b.values()]
    base, cov = bme680_v2_baseline(samples, BME_V2_WINDOW_S // 60)
    rep = bme680_air_quality_v2(m.get("gas_ohm"), m.get("temperature_c"), m.get("humidity_pct"), base, cov,
                                m.get("gas_valid", 1))
    if rep["air_quality"] is None:
        print(f"  {gid}: v2 {rep['air_quality_conf']} — {rep['explanation']}")
    return rep["air_quality"]


def _bme680_v3(conn, gid, now_ep, rungs_path=None):
    """v3 score for one BME680 node now: Sensirion's VOC Index algorithm replayed over the last 3 d of 1-min
    rung buckets (gas_ohm + die T/RH), ending on the latest raw payload when that is newer than the rung.
    Stateless — recomputed every run — so a restart or a missed run can't corrupt a saved baseline."""
    path = Path(rungs_path or RUNGS)
    if not path.exists():
        return None
    rc = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        by_b: dict[int, dict] = {}
        for b, metric, v in rc.execute(
                "SELECT bucket_start, metric, vmean FROM rung WHERE res='1min' AND device_id=? "
                "AND metric IN ('gas_ohm','temperature_c','humidity_pct') AND bucket_start >= ?",
                (gid, int(now_ep - BME_V3_WINDOW_S))):
            by_b.setdefault(b, {})[metric] = v
    finally:
        rc.close()
    samples = [(by_b[b].get("gas_ohm"), by_b[b].get("temperature_c"), by_b[b].get("humidity_pct")) for b in sorted(by_b)]
    row = conn.execute("SELECT max(ts) FROM readings WHERE device_id=? AND metric='gas_ohm' AND authoritative=1",
                       (gid,)).fetchone()
    gas_valid = 1
    if row and row[0] and (not by_b or calendar.timegm(time.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ")) >= max(by_b) + 60):
        m = dict(conn.execute("SELECT metric, value FROM readings WHERE device_id=? AND ts=? AND authoritative=1 "
                              "AND metric IN ('gas_ohm','temperature_c','humidity_pct','gas_valid')",
                              (gid, row[0])).fetchall())
        samples.append((m.get("gas_ohm"), m.get("temperature_c"), m.get("humidity_pct")))
        gas_valid = m.get("gas_valid", 1)
    idx, cov = bme680_voc_index(samples, BME_V3_WINDOW_S // 60)
    rep = bme680_air_quality_v3(idx, cov, gas_valid)
    if rep["air_quality"] is None:
        print(f"  {gid}: v3 {rep['air_quality_conf']} — {rep['explanation']}")
    return rep["air_quality"]


def _temp_source(devices, area, exclude_id):
    """A SAME-ROOM temperature for SGP compensation, or None — never another room's (that was F8's flapping
    reference). A plain T/RH meter first (_meter_rank: 'pro', then freshest); else a BME680 in the room — its die
    runs warm, but only the temperature CHANGE matters (the VOC Index algorithm is offset-free)."""
    cands = [d for d in devices if d.get("area") == area and d["device_id"] != exclude_id
             and (d.get("metrics") or {}).get("temperature_c") is not None]
    meters = [d for d in cands if "gas" not in (d.get("device_type") or "").lower()
              and "bme680" not in (d.get("device_type") or "").lower()]
    if meters:
        return min(meters, key=_meter_rank)
    bmes = [d for d in cands if "bme680" in (d.get("device_type") or "").lower()]
    return min(bmes, key=_meter_rank) if bmes else None


def _sgp_tc(gid, t_source, now_ep, rungs_path=None):
    """Phase 4a score for one SGP40/41 node: its voc_raw minus SGP_TC_K_T × the same-room temperature, through
    Sensirion's algorithm over the last 3 d of the 1-min rung. Temperature is carried forward ≤ 30 min (meters
    report every few minutes). Stateless, like v3."""
    path = Path(rungs_path or RUNGS)
    if not path.exists():
        return None
    since = int(now_ep - SGP_TC_WINDOW_S)
    q = ("SELECT bucket_start, vmean FROM rung WHERE res='1min' AND device_id=? AND metric=? AND bucket_start >= ? "
         "AND vmean IS NOT NULL ORDER BY bucket_start")
    rc = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        raw = rc.execute(q, (gid, "voc_raw", since)).fetchall()
        temp = rc.execute(q, (t_source, "temperature_c", since - 1800)).fetchall()
    finally:
        rc.close()
    samples, j, last_t, last_b = [], 0, None, None
    for b, sraw in raw:
        while j < len(temp) and temp[j][0] <= b:
            last_b, last_t = temp[j]
            j += 1
        samples.append((sraw, last_t if last_b is not None and b - last_b <= 1800 else None))
    idx, cov = sgp_tc_voc_index(samples, SGP_TC_WINDOW_S // 60)
    rep = sgp_air_quality_tc(idx, cov)
    if rep["air_quality"] is None:
        print(f"  {gid}: tc {rep['air_quality_conf']} — {rep['explanation']}")
    return rep["air_quality"]


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
