"""latest_cache — a materialized "latest authoritative reading per (device_id, metric)".

Board task `sensors-query-unbounded`. `build_sensor_list` (the PWA's `/api/v1/sensors`) used to derive
the latest value per device-metric with an **unbounded** `GROUP BY device_id, metric` over every
authoritative row in hot.db, on EVERY request. That is O(rows) work to produce ~107 rows.

It is the root of the 2026-07-27 failover outage: keepalived's fitness probe hit that endpoint, the
standby's hot.db reached 2.9M rows, the query reached 8.96s against a 4s timeout, and the air-gap VIP
could not move for four days. `c73edca` moved the PROBE off it; this removes the cost itself, which real
PWA and panel clients still pay (ha-2 measured 1.62s and climbing on 2026-08-01).

WHY A TRIGGER AND NOT APPLICATION CODE
--------------------------------------
`readings` has many writers, and they are not all Python: `server/storage/writer.py` (the live sink),
`gas_quality_persist`, `recompute_air_quality`, `edge_history` backfills, the CSV importers — and
`failover/reconcile-history.sh`, which merges a peer's rows with `INSERT OR IGNORE` through the
**`sqlite3` CLI in bash**. Maintaining the cache in the writer would therefore be silently wrong the
moment any other path inserted. A trigger lives in the database, so every writer goes through it whatever
language it is written in.

THE LOAD-BEARING DETAIL: `WHERE excluded.ts > latest_readings.ts`
-----------------------------------------------------------------
Inserts are NOT chronological. History recovery replays a meter's on-device log (2862 backdated rows
landed on ha-2 on 2026-08-01), and reconcile merges a peer's older window. Without that guard, a backfill
of last week's data would overwrite "latest" with a week-old value and the whole PWA would show stale
readings. The cache tracks the newest ts, not the most recently inserted row.

SEMANTICS ARE DELIBERATELY UNCHANGED
------------------------------------
The compactor keeps only `ts >= yesterday 00:00 UTC` in hot.db, so a device silent for days currently
drops out of the sensor list entirely. Making it linger (now that a cache could remember it) would be a
UX change smuggled inside a performance fix, so `prune()` drops entries whose newest ts fell before the
compaction cutoff — matching the old query row for row. Whether dead devices *should* stay visible is a
separate question for the gap-watcher, not for this.

Callers that need correctness above all (`build_sensor_list`) check `is_usable()` and fall back to the
original query, so a database without the migration — ha-2 until it is deployed — is never wrong, just
slower.
"""
from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger("ha.latest_cache")

# The `latest per group` result, maintained incrementally. WITHOUT ROWID: it is a small, wide-key
# lookup table (~107 rows here) and the PK *is* the access path.
DDL = """
CREATE TABLE IF NOT EXISTS latest_readings (
    device_id TEXT NOT NULL,
    metric    TEXT NOT NULL,
    value     REAL NOT NULL,
    ts        TEXT NOT NULL,
    PRIMARY KEY (device_id, metric)
) WITHOUT ROWID;

-- Fires for EVERY writer (python, bash, a human with sqlite3) because it lives in the db, not the app.
-- Only authoritative rows: the sensor view excludes device self-reports, so caching them would mean
-- filtering them back out on read.
-- An `INSERT OR IGNORE` that is actually ignored does NOT fire an AFTER INSERT trigger, so duplicate
-- merges are free.
CREATE TRIGGER IF NOT EXISTS trg_readings_latest_ins
AFTER INSERT ON readings
WHEN NEW.authoritative = 1
BEGIN
    INSERT INTO latest_readings (device_id, metric, value, ts)
    VALUES (NEW.device_id, NEW.metric, NEW.value, NEW.ts)
    ON CONFLICT(device_id, metric) DO UPDATE SET
        value = excluded.value,
        ts    = excluded.ts
    WHERE excluded.ts > latest_readings.ts;   -- never let a backfill overwrite a newer reading
END;
"""

# The original O(rows) derivation. Kept as the single definition of truth for what "latest" means, used
# for the initial backfill, for rebuild(), and by build_sensor_list's fallback — so the fast path and the
# slow path can never drift apart in meaning.
LATEST_SELECT = """
SELECT r.device_id, r.metric, r.value, r.ts
  FROM readings r
  JOIN (SELECT device_id, metric, MAX(ts) AS mts FROM readings
         WHERE authoritative=1 GROUP BY device_id, metric) m
    ON r.device_id=m.device_id AND r.metric=m.metric AND r.ts=m.mts
 WHERE r.authoritative=1
"""


def ensure(conn: sqlite3.Connection) -> None:
    """Create the table + trigger, and backfill once if the table is empty. Idempotent and cheap to call
    on every open: the backfill only runs when there is nothing there."""
    conn.executescript(DDL)
    row = conn.execute("SELECT 1 FROM latest_readings LIMIT 1").fetchone()
    if row is None:
        n = rebuild(conn)
        if n:
            log.info("latest_readings backfilled with %d device-metric pairs", n)


def rebuild(conn: sqlite3.Connection) -> int:
    """Recompute the whole cache from `readings`. Use after anything that rewrites device_ids in bulk
    (device_migrate / device_relocate) — the trigger tracks inserts, so a mass UPDATE of device_id would
    otherwise leave the old key behind. Returns the row count."""
    conn.executescript(DDL)
    with conn:
        conn.execute("DELETE FROM latest_readings")
        conn.execute(
            f"INSERT INTO latest_readings (device_id, metric, value, ts) {LATEST_SELECT}")
    return conn.execute("SELECT count(*) FROM latest_readings").fetchone()[0]


def prune(conn: sqlite3.Connection, cutoff_ts: str) -> int:
    """Drop entries whose newest reading predates the compaction cutoff, so the cache shows exactly what
    the un-cached query would have shown against the post-compaction hot.db. Returns rows removed."""
    if not _table_exists(conn):
        return 0
    with conn:
        cur = conn.execute("DELETE FROM latest_readings WHERE ts < ?", (cutoff_ts,))
    return cur.rowcount or 0


def _table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='latest_readings'").fetchone() is not None


def is_usable(conn: sqlite3.Connection) -> bool:
    """True iff the cache can be trusted to answer for this database.

    Requires the table, the trigger, AND at least one row. An empty cache on a database that HAS readings
    means the migration has not backfilled yet (or someone truncated it), and answering from it would
    silently return an empty sensor list — far worse than being slow. Callers fall back instead.
    """
    if not _table_exists(conn):
        return False
    trig = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_readings_latest_ins'").fetchone()
    if trig is None:
        return False                      # table without the trigger = frozen, would go stale unnoticed
    return conn.execute("SELECT 1 FROM latest_readings LIMIT 1").fetchone() is not None


def compare_with_source(conn: sqlite3.Connection) -> tuple[int, list]:
    """Audit: (#differences, sample). Compares the cache against the O(rows) derivation it replaces.
    Used by the tests and by tools/rebuild_latest_readings.py --verify to prove the fast path agrees with
    the slow one on real data, rather than asserting it.

    ONE statement, deliberately. Reading the two sides as separate queries races against live ingest: on
    ha-2 (~1 reading/s) rows land between them and the cache legitimately comes back NEWER than the
    "expected" snapshot, so the audit cries drift on a perfectly healthy database. Found doing exactly
    that during the 2026-08-01 ha-2 deploy. A single statement sees one consistent snapshot.
    """
    sql = f"""
    WITH src AS ({LATEST_SELECT})
    SELECT COALESCE(s.device_id, l.device_id), COALESCE(s.metric, l.metric),
           s.value, s.ts, l.value, l.ts
      FROM src s LEFT JOIN latest_readings l
        ON l.device_id = s.device_id AND l.metric = s.metric
     WHERE l.device_id IS NULL OR l.value IS NOT s.value OR l.ts IS NOT s.ts
    UNION ALL
    SELECT l.device_id, l.metric, NULL, NULL, l.value, l.ts
      FROM latest_readings l LEFT JOIN src s
        ON s.device_id = l.device_id AND s.metric = l.metric
     WHERE s.device_id IS NULL
    """
    diffs = [{"key": (d, m),
              "expected": None if st is None else (sv, st),
              "cached": None if lt is None else (lv, lt)}
             for d, m, sv, st, lv, lt in conn.execute(sql)]
    return len(diffs), diffs[:20]
