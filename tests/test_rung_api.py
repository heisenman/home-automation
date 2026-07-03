"""ADR-0022 Phase 2 API surface (server/api/main.py): the rung resolution-selector on
/devices/{id}/readings (_resolve_rung_res + _rung_query) and the incremental /api/v1/rung/since NDJSON
stream. The query bodies are tested directly (no lifespan/TestClient) against a synthetic rungs.db."""
import asyncio
import json
import sqlite3

import server.api.main as M
from server.storage import rollup as R


def _mk_rungs(tmp_path):
    """A tiny rungs.db: 3 hourly buckets + 1 daily bucket for device d1 / metric t."""
    p = tmp_path / "rungs.db"
    c = sqlite3.connect(str(p))
    c.execute(R.RUNG_DDL)
    base = R.epoch_of("2026-05-01T00:00:00Z")
    rows = [("1hour", "d1", "t", base + i * 3600, float(i), float(i) + 10, float(i) + 5, 60, float(i) + 9)
            for i in range(3)]
    rows.append(("1day", "d1", "t", base, 0.0, 30.0, 15.0, 180, 29.0))
    c.executemany(R._UPSERT, rows)
    c.commit()
    c.close()
    return p


async def _aread(it):
    return [(x.decode() if isinstance(x, (bytes, bytearray)) else x) async for x in it]


# ── _resolve_rung_res: span → rung (mirrors rollup.select_resolution) ──────────────────────────────────
def test_resolve_rung_res_auto_and_explicit():
    assert M._resolve_rung_res("auto", "2026-01-01T00:00:00Z", "2026-01-01T00:30:00Z") == "raw"     # 30m
    assert M._resolve_rung_res("auto", "2025-01-01T00:00:00Z", "2026-06-01T00:00:00Z") == "1day"    # ~1.4y
    assert M._resolve_rung_res("1week", "a", "b") == "1week"                                         # forced
    assert M._resolve_rung_res("nonsense", "a", "b") == "raw"                                        # bad → raw


# ── _rung_query: aggregate points + calibration ───────────────────────────────────────────────────────
def test_rung_query_returns_mean_min_max_count(tmp_path):
    p = _mk_rungs(tmp_path)
    old_db, old_cal = M.RUNG_DB, M._device_calibration
    M.RUNG_DB, M._device_calibration = p, lambda d: {}
    try:
        out = M._rung_query("d1", "2026-05-01T00:00:00Z", "2026-05-01T05:00:00Z", None, "1hour", 1000)
    finally:
        M.RUNG_DB, M._device_calibration = old_db, old_cal
    assert out["resolution"] == "1hour" and out["rows"] == 3 and out["truncated"] is False
    r0 = out["readings"][0]
    assert (r0["ts"], r0["metric"], r0["value"], r0["min"], r0["max"], r0["count"]) == \
           ("2026-05-01T00:00:00Z", "t", 5.0, 0.0, 10.0, 60)


def test_rung_query_applies_calibration_offset(tmp_path):
    p = _mk_rungs(tmp_path)
    old_db, old_cal = M.RUNG_DB, M._device_calibration
    M.RUNG_DB, M._device_calibration = p, lambda d: {"t": 100.0}
    try:
        out = M._rung_query("d1", "2026-05-01T00:00:00Z", "2026-05-01T05:00:00Z", None, "1hour", 1000)
    finally:
        M.RUNG_DB, M._device_calibration = old_db, old_cal
    r0 = out["readings"][0]
    assert r0["value"] == 105.0 and r0["min"] == 100.0 and r0["max"] == 110.0   # offset on value+min+max


def test_rung_query_none_when_db_absent(tmp_path):
    old = M.RUNG_DB
    M.RUNG_DB = tmp_path / "nope.db"
    try:
        assert M._rung_query("d1", "x", "y", None, "1hour", 10) is None          # caller falls back to raw
    finally:
        M.RUNG_DB = old


# ── /api/v1/rung/since: inclusive-HWM NDJSON incremental ───────────────────────────────────────────────
def test_rung_since_streams_ndjson_inclusive_of_after(tmp_path):
    p = _mk_rungs(tmp_path)
    base = R.epoch_of("2026-05-01T00:00:00Z")
    old = M.RUNG_DB
    M.RUNG_DB = p
    try:
        resp = M.rung_since(res="1hour", after=base + 3600, limit=1000)
        chunks = asyncio.run(_aread(resp.body_iterator))
    finally:
        M.RUNG_DB = old
    objs = [json.loads(ln) for ln in "".join(chunks).splitlines() if ln]
    assert [o["bucket_start"] for o in objs] == [base + 3600, base + 7200]       # >= after (inclusive)
    assert all(o["res"] == "1hour" and o["device_id"] == "d1" and o["metric"] == "t" for o in objs)
    assert objs[0]["vmean"] == 6.0 and objs[0]["vcount"] == 60                    # raw rung values (no calib)


def test_rung_since_rejects_bad_res(tmp_path):
    from fastapi import HTTPException

    from tests._harness import raises
    with raises(HTTPException):
        M.rung_since(res="5min", after=0, limit=10)                              # not a rung → 400
