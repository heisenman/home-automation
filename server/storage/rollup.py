"""Multi-resolution rollup ladder (ADR-0022) — the canonical long-history representation.

Rolls raw readings up a {1min, 1hour, 1day, 1week} ladder of (min,max,mean,count,last) aggregates.
Rungs live in a compact sqlite (`rungs.db`, MB-scale) — the ONLY history the panel/standby replicate;
raw stays as parquet (server-only). Query picks a rung by span so charts stay pixel-identical to raw.

Design: **recompute-and-replace per bucket**. Each run recomputes every bucket touched by new source
data *fully* from its source and upserts a full replacement — so a re-run is idempotent and self-heals a
partial bucket (no fragile incremental merge of mean/last). Work is bounded to recently-touched buckets.

Contract + schema: docs/design/rollup-ladder-and-replica-sync.md (PK order frozen Order B, ops-validated).
Composition (ADR-0022): 1hour←60×1min, 1day←24×1hour, 1week←7×1day;
  vmin=min(vmin), vmax=max(vmax), vcount=Σvcount, vmean=Σ(vmean·vcount)/Σvcount, vlast=last-by-bucket.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

log = logging.getLogger("ha.rollup")

RUNG_DDL = """
CREATE TABLE IF NOT EXISTS rung (
    res          TEXT    NOT NULL,
    device_id    TEXT    NOT NULL,
    metric       TEXT    NOT NULL,
    bucket_start INTEGER NOT NULL,
    vmin  REAL, vmax REAL, vmean REAL, vcount INTEGER, vlast REAL,
    PRIMARY KEY (res, device_id, metric, bucket_start)
) WITHOUT ROWID;
"""
_META_DDL = "CREATE TABLE IF NOT EXISTS rollup_meta (k TEXT PRIMARY KEY, v TEXT);"

WIDTH_S = {"1min": 60, "1hour": 3600, "1day": 86400, "1week": 604800}
CASCADE = [("1hour", "1min"), ("1day", "1hour"), ("1week", "1day")]   # (dest, src), bottom-up
# 1min tuned DOWN from the ADR-0022 default (90d) to 7d: sampling here is ~1/min, so the 1min rung barely
# compacts vs raw — 90d of it made the panel-replica rungs.db ~700MB (not the "MB-scale" the ladder promises).
# A wall panel only needs 1min detail for recent ≤2d zoom; 1hour covers older spans legibly. Tunable knob.
RETENTION_DAYS = {"1min": 7, "1hour": 730}                             # 1day / 1week kept forever

# ── resolution selector ─────────────────────────────────────────────────────────────────────────────
# Pick the rung whose points-per-chart stays legible for a given query span. Kept here (not in the API) so
# the panel's ha_replica mirrors the SAME span→rung mapping and charts render identically off the replica.
# Ordered; first `span_s <= limit` wins. "raw" = full-resolution hot+parquet (exact, no rung).
RESOLUTION_LADDER = [
    (2 * 3600,        "raw"),      # ≤2h  → raw points (exact)
    (2 * 86400,       "1min"),     # ≤2d  → 1-min rung  (≤2880 pts)
    (60 * 86400,      "1hour"),    # ≤2mo → 1-hour rung (≤1440 pts)
    (4 * 365 * 86400, "1day"),     # ≤4y  → 1-day rung  (≤1460 pts)
]
DEFAULT_LONG_RES = "1week"                                             # beyond 4y


def select_resolution(span_s: float) -> str:
    """span (seconds) → rung name (or 'raw'). The single source of truth for span→rung; API + panel mirror it."""
    for limit, res in RESOLUTION_LADDER:
        if span_s <= limit:
            return res
    return DEFAULT_LONG_RES


# ── time helpers ──────────────────────────────────────────────────────────────
def epoch_of(ts_iso: str) -> int:
    """ISO-8601 (…Z) → epoch seconds (UTC)."""
    return int(datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp())


def iso_of(epoch: int) -> str:
    """epoch seconds (UTC) → ISO-8601 …Z (inverse of epoch_of; used to label rung bucket_start for clients)."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dest_bucket(src_epoch: int, dest_res: str) -> int:
    """The dest-rung bucket_start that a source bucket_start falls into."""
    if dest_res == "1hour":
        return src_epoch // 3600 * 3600
    dt = datetime.fromtimestamp(src_epoch, timezone.utc)
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if dest_res == "1day":
        return int(day.timestamp())
    if dest_res == "1week":                       # Monday 00:00 UTC (weekday(): Mon=0)
        return int((day - timedelta(days=dt.weekday())).timestamp())
    raise ValueError(dest_res)


# ── pure aggregation (the correctness-critical bits; unit-tested) ───────────────
def aggregate_raw(rows: list[tuple]) -> dict[tuple, tuple]:
    """rows = [(ts_iso, device_id, metric, value)] → {(device_id,metric,minute_start): agg}.
    agg = (vmin, vmax, vmean, vcount, vlast). `vlast` is the value at the latest ts in the bucket."""
    acc: dict[tuple, dict] = {}
    for ts, device_id, metric, value in rows:
        if value is None:
            continue
        v = float(value)
        b = epoch_of(ts) // 60 * 60
        key = (device_id, metric, b)
        a = acc.get(key)
        if a is None:
            acc[key] = {"min": v, "max": v, "sum": v, "n": 1, "last_ts": ts, "last": v}
        else:
            a["min"] = min(a["min"], v); a["max"] = max(a["max"], v)
            a["sum"] += v; a["n"] += 1
            if ts >= a["last_ts"]:
                a["last_ts"] = ts; a["last"] = v
    return {k: (a["min"], a["max"], a["sum"] / a["n"], a["n"], a["last"]) for k, a in acc.items()}


def compose(src_rows: list[tuple]) -> tuple:
    """Compose one dest bucket from its source rung rows.
    src_rows = [(bucket_start, vmin, vmax, vmean, vcount, vlast)] → (vmin,vmax,vmean,vcount,vlast)."""
    vmin = min(r[1] for r in src_rows)
    vmax = max(r[2] for r in src_rows)
    total = sum(r[4] for r in src_rows)
    vmean = sum(r[3] * r[4] for r in src_rows) / total if total else None
    vlast = max(src_rows, key=lambda r: r[0])[5]           # last = latest source bucket's last
    return (vmin, vmax, vmean, total, vlast)


# ── sqlite ops ─────────────────────────────────────────────────────────────────
def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(RUNG_DDL)
    conn.execute(_META_DDL)
    conn.commit()


_UPSERT = """
INSERT INTO rung (res, device_id, metric, bucket_start, vmin, vmax, vmean, vcount, vlast)
VALUES (?,?,?,?,?,?,?,?,?)
ON CONFLICT(res, device_id, metric, bucket_start) DO UPDATE SET
  vmin=excluded.vmin, vmax=excluded.vmax, vmean=excluded.vmean,
  vcount=excluded.vcount, vlast=excluded.vlast
"""


def _meta_get(conn, k, default=None):
    r = conn.execute("SELECT v FROM rollup_meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def _meta_set(conn, k, v):
    conn.execute("INSERT INTO rollup_meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 (k, str(v)))


def rollup_minute(raw_conn: sqlite3.Connection, rung_conn: sqlite3.Connection,
                  since_ts: str, until_ts: str) -> int:
    """Recompute 1min buckets from raw readings in [since_ts, until_ts) and upsert. Returns rows written."""
    rows = raw_conn.execute(
        "SELECT ts, device_id, metric, value FROM readings WHERE ts >= ? AND ts < ? ORDER BY ts",
        (since_ts, until_ts)).fetchall()
    aggs = aggregate_raw(rows)
    payload = [("1min", dev, met, b, *agg) for (dev, met, b), agg in aggs.items()]
    if payload:
        rung_conn.executemany(_UPSERT, payload)
        rung_conn.commit()
    return len(payload)


def cascade(rung_conn: sqlite3.Connection, dest_res: str, src_res: str,
            lo_bucket: int, hi_bucket: int) -> int:
    """Recompute dest_res buckets whose source (src_res) buckets fall in [lo,hi]. Returns rows written."""
    src = rung_conn.execute(
        "SELECT device_id, metric, bucket_start, vmin, vmax, vmean, vcount, vlast "
        "FROM rung WHERE res=? AND bucket_start >= ? AND bucket_start < ? ",
        (src_res, lo_bucket, hi_bucket)).fetchall()
    groups: dict[tuple, list] = {}
    for dev, met, b, vmin, vmax, vmean, vcount, vlast in src:
        db = dest_bucket(b, dest_res)
        groups.setdefault((dev, met, db), []).append((b, vmin, vmax, vmean, vcount, vlast))
    payload = [(dest_res, dev, met, db, *compose(rows)) for (dev, met, db), rows in groups.items()]
    if payload:
        rung_conn.executemany(_UPSERT, payload)
        rung_conn.commit()
    return len(payload)


def prune(rung_conn: sqlite3.Connection, now_epoch: int) -> int:
    """Enforce per-rung retention (ADR-0022 defaults). 1day/1week kept forever. Returns rows deleted."""
    deleted = 0
    for res, days in RETENTION_DAYS.items():
        cutoff = now_epoch - days * 86400
        cur = rung_conn.execute("DELETE FROM rung WHERE res=? AND bucket_start < ?", (res, cutoff))
        deleted += cur.rowcount
    rung_conn.commit()
    return deleted


def run(raw_conn: sqlite3.Connection, rung_conn: sqlite3.Connection, *, now_epoch: int,
        full: bool = False) -> dict:
    """One incremental pass: raw→1min, cascade up the ladder, prune. Idempotent.
    `full=True` reprocesses from epoch 0 (initial seed). Returns a per-stage rowcount summary."""
    ensure_schema(rung_conn)
    # window: from the last complete minute we processed (reprocessed fully; idempotent) to now.
    until = now_epoch // 60 * 60
    until_iso = datetime.fromtimestamp(until, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if full:
        since_iso = "0000-01-01T00:00:00Z"
    else:
        since_iso = _meta_get(rung_conn, "last_min_since", "0000-01-01T00:00:00Z")

    n_min = rollup_minute(raw_conn, rung_conn, since_iso, until_iso)
    _meta_set(rung_conn, "last_min_since", until_iso)

    # cascade only the time-range we just touched (with a margin), bottom-up.
    lo = epoch_of(since_iso) if not full else 0
    lo = lo - WIDTH_S["1week"]            # margin so a boundary dest bucket is fully recomposed
    hi = until + 1
    out = {"1min": n_min}
    for dest_res, src_res in CASCADE:
        out[dest_res] = cascade(rung_conn, dest_res, src_res, max(lo, 0), hi)
    out["pruned"] = prune(rung_conn, now_epoch)
    _meta_set(rung_conn, "last_run", until_iso)
    rung_conn.commit()
    log.info("rollup pass: %s", out)
    return out


def run_paths(raw_db: str, rung_db: str, *, now_epoch: int | None = None, full: bool = False) -> dict:
    """Convenience: open the raw (hot.db) + rung dbs by path and run one pass."""
    import time
    raw = sqlite3.connect(raw_db)
    rung = sqlite3.connect(rung_db)
    try:
        return run(raw, rung, now_epoch=now_epoch if now_epoch is not None else int(time.time()), full=full)
    finally:
        raw.close()
        rung.close()


# ── parquet backfill (one-time history seed) ──────────────────────────────────────────────────────────
def _month_windows(lo_epoch: int, hi_epoch: int):
    """Yield [start, next_month_start) epoch pairs covering [lo,hi). A calendar-month boundary never splits
    an hour or a day, so composing 1hour←1min one month at a time is exact — this bounds cascade memory."""
    dt = datetime.fromtimestamp(lo_epoch, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while dt.timestamp() < hi_epoch:
        nxt = dt.replace(year=dt.year + 1, month=1) if dt.month == 12 else dt.replace(month=dt.month + 1)
        yield int(dt.timestamp()), int(nxt.timestamp())
        dt = nxt


def backfill_from_parquet(glob_pattern: str, rung_conn: sqlite3.Connection, *, now_epoch: int,
                          log_fn=log.info) -> dict:
    """Seed the full history from the parquet archive (server/storage/compactor.py output). Reads one file
    (≈one month) at a time so the raw→1min aggregation is memory-bounded, upserts 1min, then cascades up the
    ladder and prunes. Idempotent (recompute-and-replace per bucket). Sets `last_min_since` to the parquet
    high-water-mark so a subsequent incremental run() tops up the gap to now from hot.db."""
    import glob as _glob
    import pyarrow.parquet as pq

    ensure_schema(rung_conn)
    files = sorted(f for f in _glob.glob(glob_pattern, recursive=True) if f.endswith(".parquet"))
    total_min, max_bucket = 0, 0
    for path in files:
        # ParquetFile (not read_table): read THIS file only. read_table would infer hive partitioning from
        # the year=/month= path and try to merge every sibling's schema — the archive's `year` column dtype
        # varies across files (int64 vs dictionary), which makes that merge raise.
        t = pq.ParquetFile(path).read(columns=["ts", "device_id", "metric", "value"])
        rows = list(zip(t.column("ts").to_pylist(), t.column("device_id").to_pylist(),
                        t.column("metric").to_pylist(), t.column("value").to_pylist()))
        aggs = aggregate_raw(rows)
        payload = [("1min", dev, met, b, *agg) for (dev, met, b), agg in aggs.items()]
        if payload:
            rung_conn.executemany(_UPSERT, payload)
            rung_conn.commit()
            total_min += len(payload)
            max_bucket = max(max_bucket, max(p[3] for p in payload))
        log_fn("backfill %s: %d raw → %d 1min buckets", path, len(rows), len(payload))

    hi = now_epoch // 60 * 60 + 1
    out = {"files": len(files), "1min": total_min}
    lo = rung_conn.execute("SELECT MIN(bucket_start) FROM rung WHERE res='1min'").fetchone()[0] or 0
    # 1hour←1min per calendar month (exact + bounded); 1day/1week whole-range (their sources are small).
    n_hour = 0
    for m_lo, m_hi in _month_windows(lo, hi):
        n_hour += cascade(rung_conn, "1hour", "1min", m_lo, m_hi)
    out["1hour"] = n_hour
    out["1day"] = cascade(rung_conn, "1day", "1hour", 0, hi)
    out["1week"] = cascade(rung_conn, "1week", "1day", 0, hi)
    out["pruned"] = prune(rung_conn, now_epoch)
    if max_bucket:
        _meta_set(rung_conn, "last_min_since", iso_of(max_bucket + 60))   # next incremental starts just past
    _meta_set(rung_conn, "last_run", iso_of(now_epoch))
    rung_conn.commit()
    # Backfill churns millions of transient 1min rows that prune deletes — VACUUM reclaims the freed pages so
    # the replicated file is MB-scale, not stuck at its high-water-mark. One-time seed only; the ~5min
    # incremental run() never vacuums (too costly per tick).
    rung_conn.execute("VACUUM")
    out["vacuumed"] = True
    log_fn("backfill complete: %s", out)
    return out


def _main() -> int:
    import argparse
    import time
    p = argparse.ArgumentParser(description="ADR-0022 rollup ladder: build/refresh rungs.db from raw readings")
    p.add_argument("--raw", default="instance/db/hot.db", help="raw readings sqlite (hot.db)")
    p.add_argument("--rung", default="instance/db/rungs.db", help="output rung sqlite")
    p.add_argument("--full", action="store_true", help="reprocess from epoch 0 (initial seed)")
    p.add_argument("--parquet", nargs="?", const="instance/db/parquet/year=*/month=*/*.parquet", default=None,
                   help="one-time history seed from the parquet archive (glob), then top up from --raw hot.db")
    p.add_argument("--now", type=int, default=None, help="override now (epoch s); default = wall clock")
    p.add_argument("--log-level", default="INFO")
    a = p.parse_args()
    logging.basicConfig(level=getattr(logging, a.log_level), format="%(asctime)s %(levelname)s %(name)s — %(message)s")
    now_epoch = a.now if a.now is not None else int(time.time())
    t0 = time.time()
    if a.parquet:
        rung = sqlite3.connect(a.rung)
        try:
            bout = backfill_from_parquet(a.parquet, rung, now_epoch=now_epoch)
        finally:
            rung.close()
        # top up the parquet→now gap from the hot tier (parquet lags live by the compaction interval).
        tout = run_paths(a.raw, a.rung, now_epoch=now_epoch, full=False)
        print(f"rollup PARQUET-BACKFILL {a.parquet} + hot {a.raw} -> {a.rung} in {time.time()-t0:.1f}s: "
              f"backfill={bout} topup={tout}")
        return 0
    out = run_paths(a.raw, a.rung, now_epoch=now_epoch, full=a.full)
    print(f"rollup {'FULL' if a.full else 'incremental'} {a.raw} -> {a.rung} in {time.time()-t0:.1f}s: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
