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
RETENTION_DAYS = {"1min": 90, "1hour": 730}                            # 1day / 1week kept forever


# ── time helpers ──────────────────────────────────────────────────────────────
def epoch_of(ts_iso: str) -> int:
    """ISO-8601 (…Z) → epoch seconds (UTC)."""
    return int(datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp())


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
