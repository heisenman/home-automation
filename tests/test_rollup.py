"""ADR-0022 rollup ladder engine (server/storage/rollup.py) — aggregation, composition, alignment,
idempotency, retention, and an end-to-end raw→1min→1hour→1day ladder pass."""
import sqlite3
from datetime import datetime, timezone

from server.storage import rollup as R


def _readings_db(rows):
    """In-memory hot.db with a minimal readings table. rows = [(ts_iso, device_id, metric, value)]."""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE readings (ts TEXT, device_id TEXT, metric TEXT, value REAL)")
    c.executemany("INSERT INTO readings VALUES (?,?,?,?)", rows)
    c.commit()
    return c


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── time alignment ──────────────────────────────────────────────────────────────────────────────
def test_dest_bucket_hour_day_alignment():
    e = R.epoch_of("2026-07-02T15:37:45Z")
    assert R.dest_bucket(e, "1hour") == R.epoch_of("2026-07-02T15:00:00Z")
    assert R.dest_bucket(e, "1day") == R.epoch_of("2026-07-02T00:00:00Z")


def test_dest_bucket_week_is_monday_and_contiguous():
    e = R.epoch_of("2026-07-02T15:37:45Z")            # a Thursday
    wk = R.dest_bucket(e, "1week")
    assert datetime.fromtimestamp(wk, timezone.utc).weekday() == 0        # Monday
    assert wk <= e and e - wk < 604800
    # a timestamp one week later lands in the adjacent week bucket
    assert R.dest_bucket(e + 604800, "1week") == wk + 604800


# ── pure aggregation ─────────────────────────────────────────────────────────────────────────────
def test_aggregate_raw_minute_bucket_stats_and_last():
    base = R.epoch_of("2026-07-02T00:00:00Z")
    rows = [                                            # deliberately OUT of ts order
        (_iso(base + 40), "d1", "t", 30.0),
        (_iso(base + 0),  "d1", "t", 10.0),
        (_iso(base + 20), "d1", "t", 20.0),
        (_iso(base + 61), "d1", "t", 99.0),             # next minute
    ]
    agg = R.aggregate_raw(rows)
    vmin, vmax, vmean, vcount, vlast = agg[("d1", "t", base)]
    assert (vmin, vmax, vcount) == (10.0, 30.0, 3)
    assert abs(vmean - 20.0) < 1e-9
    assert vlast == 30.0                                # value at the latest ts in the minute
    assert agg[("d1", "t", base + 60)] == (99.0, 99.0, 99.0, 1, 99.0)


def test_aggregate_raw_skips_none_and_splits_series():
    base = R.epoch_of("2026-07-02T00:00:00Z")
    rows = [(_iso(base), "d1", "t", None), (_iso(base), "d1", "t", 5.0), (_iso(base), "d2", "h", 8.0)]
    agg = R.aggregate_raw(rows)
    assert agg[("d1", "t", base)] == (5.0, 5.0, 5.0, 1, 5.0)     # None skipped
    assert ("d2", "h", base) in agg                              # distinct series kept apart


def test_compose_is_count_weighted():
    # bucket A: mean 10 over 3 samples; bucket B: mean 20 over 1 sample → weighted mean = 12.5
    src = [(0, 8.0, 12.0, 10.0, 3, 11.0), (60, 20.0, 20.0, 20.0, 1, 20.0)]
    vmin, vmax, vmean, vcount, vlast = R.compose(src)
    assert vmin == 8.0 and vmax == 20.0 and vcount == 4
    assert abs(vmean - 12.5) < 1e-9
    assert vlast == 20.0                                # last = latest source bucket's last


# ── end-to-end ladder ────────────────────────────────────────────────────────────────────────────
def _seed_two_hours():
    """One device, a reading every 30 s for 2 h → 4 raw/min, 60 min/hour."""
    base = R.epoch_of("2026-07-02T00:00:00Z")
    rows = []
    for i in range(2 * 60 * 2):                         # 2 h × 60 min × 2 per min
        t = base + i * 30
        rows.append((_iso(t), "d1", "t", float(i % 50)))
    return base, rows


def test_end_to_end_ladder_and_hierarchical_consistency():
    base, rows = _seed_two_hours()
    raw = _readings_db(rows)
    rung = sqlite3.connect(":memory:")
    now = base + 2 * 3600 + 120                          # just past the 2h window
    out = R.run(raw, rung, now_epoch=now, full=True)
    assert out["1min"] == 120 and out["1hour"] == 2 and out["1day"] == 1

    # a 1hour bucket's count == sum of its 60 one-min counts (== 120 readings/hour)
    h0 = rung.execute("SELECT vcount FROM rung WHERE res='1hour' AND bucket_start=?", (base,)).fetchone()
    assert h0[0] == 120
    # 1hour mean == count-weighted mean of the 1min means (hierarchically composable)
    mins = rung.execute("SELECT vmean, vcount FROM rung WHERE res='1min' AND bucket_start>=? AND bucket_start<?",
                        (base, base + 3600)).fetchall()
    expect = sum(m * n for m, n in mins) / sum(n for _, n in mins)
    hmean = rung.execute("SELECT vmean FROM rung WHERE res='1hour' AND bucket_start=?", (base,)).fetchone()[0]
    assert abs(hmean - expect) < 1e-9
    # day min/max envelope preserved from raw
    day = rung.execute("SELECT vmin, vmax FROM rung WHERE res='1day'").fetchone()
    assert day == (0.0, 49.0)


def test_run_is_idempotent():
    base, rows = _seed_two_hours()
    raw = _readings_db(rows)
    rung = sqlite3.connect(":memory:")
    now = base + 2 * 3600 + 120
    R.run(raw, rung, now_epoch=now, full=True)
    snap1 = rung.execute("SELECT res,device_id,metric,bucket_start,vmin,vmax,vmean,vcount,vlast "
                         "FROM rung ORDER BY res,bucket_start").fetchall()
    R.run(raw, rung, now_epoch=now, full=True)           # re-run
    snap2 = rung.execute("SELECT res,device_id,metric,bucket_start,vmin,vmax,vmean,vcount,vlast "
                         "FROM rung ORDER BY res,bucket_start").fetchall()
    assert snap1 == snap2                                # no duplication, no drift


# ── resolution selector (span → rung) ─────────────────────────────────────────────────────────────────
def test_select_resolution_span_ladder():
    assert R.select_resolution(3600) == "raw"                # 1h
    assert R.select_resolution(2 * 3600) == "raw"            # 2h boundary inclusive
    assert R.select_resolution(2 * 3600 + 1) == "1min"       # just over 2h
    assert R.select_resolution(2 * 86400) == "1min"          # 2d boundary
    assert R.select_resolution(7 * 86400) == "1hour"         # a week
    assert R.select_resolution(60 * 86400) == "1hour"        # 2mo boundary
    assert R.select_resolution(365 * 86400) == "1day"        # a year
    assert R.select_resolution(4 * 365 * 86400) == "1day"    # 4y boundary
    assert R.select_resolution(10 * 365 * 86400) == "1week"  # decade → coarsest


def test_iso_epoch_roundtrip():
    e = R.epoch_of("2026-07-02T15:37:00Z")
    assert R.iso_of(e) == "2026-07-02T15:37:00Z"


# ── parquet backfill ──────────────────────────────────────────────────────────────────────────────────
def test_backfill_from_parquet_seeds_ladder_and_hwm(tmp_path):
    pq = __import__("importlib").import_module("pyarrow.parquet")
    pa = __import__("importlib").import_module("pyarrow")
    base = R.epoch_of("2026-05-01T00:00:00Z")                # inside one month
    rows = [(_iso(base + i * 30), "d1", "t", float(i % 50)) for i in range(2 * 60 * 2)]   # 2h @30s
    tbl = pa.table({"ts": [r[0] for r in rows], "device_id": [r[1] for r in rows],
                    "metric": [r[2] for r in rows], "value": [r[3] for r in rows]})
    pdir = tmp_path / "year=2026" / "month=05"
    pdir.mkdir(parents=True)
    pq.write_table(tbl, str(pdir / "2026-05.parquet"))

    rung = sqlite3.connect(":memory:")
    now = base + 2 * 3600 + 120
    out = R.backfill_from_parquet(str(tmp_path / "year=*" / "month=*" / "*.parquet"), rung,
                                  now_epoch=now, log_fn=lambda *a: None)
    assert out["files"] == 1 and out["1min"] == 120 and out["1hour"] == 2 and out["1day"] == 1
    # hierarchical consistency survives the parquet path: 1hour count == 120 readings/hour
    h0 = rung.execute("SELECT vcount FROM rung WHERE res='1hour' AND bucket_start=?", (base,)).fetchone()
    assert h0[0] == 120
    # HWM set so a later incremental continues just past the last seeded minute
    hwm = rung.execute("SELECT v FROM rollup_meta WHERE k='last_min_since'").fetchone()[0]
    assert R.epoch_of(hwm) == base + (2 * 3600 - 60) + 60    # last bucket_start + 60


def test_prune_drops_old_minute_keeps_day():
    rung = sqlite3.connect(":memory:")
    R.ensure_schema(rung)
    now = R.epoch_of("2026-07-02T00:00:00Z")
    old = now - 100 * 86400                              # 100 days old (> 90d 1min retention)
    rung.executemany(R._UPSERT, [
        ("1min", "d1", "t", old, 1, 1, 1, 1, 1),
        ("1min", "d1", "t", now - 60, 1, 1, 1, 1, 1),    # recent
        ("1day", "d1", "t", old, 1, 1, 1, 1, 1),         # day rung kept forever
    ])
    rung.commit()
    R.prune(rung, now)
    kept = {(r[0], r[1]) for r in rung.execute("SELECT res, bucket_start FROM rung")}
    assert ("1min", old) not in kept                     # pruned
    assert ("1min", now - 60) in kept and ("1day", old) in kept
