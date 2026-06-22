"""Persistence for observed mesh links + pull outcomes (hot.db). Self-contained: creates its own
tables idempotently, so it needs no change to the writer service. The pure graph lives in topology;
this only reads/writes the facts that feed it.

Tables:
  mesh_links(src_kind, src_id, dst_kind, dst_id, link_kind, rssi, n_ok, n_fail, last_ts)
    one row per observed directed edge. record_link() upserts and bumps the ok/fail counters.
  pull_log(ts, device_id, path, ok, n_samples, reason)
    append-only audit of every history-pull attempt; pull_stats() aggregates it for routing.
"""
from __future__ import annotations

import sqlite3
import time

from server.mesh import topology as T

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mesh_links (
    src_kind  TEXT NOT NULL,
    src_id    TEXT NOT NULL,
    dst_kind  TEXT NOT NULL,
    dst_id    TEXT NOT NULL,
    link_kind TEXT NOT NULL,
    rssi      INTEGER,
    n_ok      INTEGER NOT NULL DEFAULT 0,
    n_fail    INTEGER NOT NULL DEFAULT 0,
    last_ts   TEXT NOT NULL,
    PRIMARY KEY (src_kind, src_id, dst_kind, dst_id, link_kind)
);
CREATE TABLE IF NOT EXISTS pull_log (
    ts         TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    path       TEXT,
    ok         INTEGER NOT NULL,
    n_samples  INTEGER NOT NULL DEFAULT 0,
    reason     TEXT
);
CREATE INDEX IF NOT EXISTS idx_pull_log_dev ON pull_log (device_id, ts);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def record_link(conn, src, dst, link_kind, rssi=None, ok: bool | None = None, ts: str | None = None):
    """Upsert an observed edge. src/dst are (kind, id) tuples. ok: True bumps n_ok, False bumps n_fail,
    None just refreshes rssi/last_ts (a passive sighting)."""
    ts = ts or _now_iso()
    d_ok = 1 if ok is True else 0
    d_fail = 1 if ok is False else 0
    conn.execute(
        """INSERT INTO mesh_links (src_kind, src_id, dst_kind, dst_id, link_kind, rssi, n_ok, n_fail, last_ts)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(src_kind, src_id, dst_kind, dst_id, link_kind) DO UPDATE SET
             rssi    = COALESCE(excluded.rssi, mesh_links.rssi),
             n_ok    = mesh_links.n_ok   + ?,
             n_fail  = mesh_links.n_fail + ?,
             last_ts = excluded.last_ts""",
        (src[0], src[1], dst[0], dst[1], link_kind, rssi, d_ok, d_fail, ts, d_ok, d_fail))
    conn.commit()


def record_pull(conn, device_id, path, ok: bool, n_samples: int = 0, reason: str = "", ts=None):
    """Append a history-pull outcome. `path` is a serialized hop chain (or just the puller id)."""
    conn.execute("INSERT INTO pull_log (ts, device_id, path, ok, n_samples, reason) VALUES (?,?,?,?,?,?)",
                 (ts or _now_iso(), device_id, path, 1 if ok else 0, int(n_samples), reason))
    conn.commit()


def _age_s(last_ts: str, now: float) -> float:
    try:
        t = time.mktime(time.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        return max(0.0, now - t)
    except Exception:
        return 0.0


def load_links(conn, now: float | None = None):
    """Read mesh_links into topology.Link objects (age computed from last_ts)."""
    now = now if now is not None else time.time()
    out = []
    for r in conn.execute("""SELECT src_kind, src_id, dst_kind, dst_id, link_kind, rssi, n_ok, n_fail, last_ts
                             FROM mesh_links"""):
        out.append(T.Link(src=(r[0], r[1]), dst=(r[2], r[3]), kind=r[4], rssi=r[5],
                          n_ok=r[6], n_fail=r[7], age_s=_age_s(r[8], now)))
    return out


def pull_stats(conn):
    """(receiver_id, device_id) -> (n_ok, n_fail) from pull_log. The receiver is the LAST hop in the
    recorded path (the node that actually held the GATT connection)."""
    stats: dict = {}
    for ts, device_id, path, ok in conn.execute("SELECT ts, device_id, path, ok FROM pull_log"):
        recv = _terminal_receiver(path)
        if recv is None:
            continue
        o, f = stats.get((recv, device_id), (0, 0))
        stats[(recv, device_id)] = (o + (1 if ok else 0), f + (0 if ok else 1))
    return stats


def _terminal_receiver(path: str | None):
    """The receiver that held the GATT link = the hop just before the endpoint in a serialized path
    like 'server:server>node:c6-bench>meter_pro_x'. Falls back to the whole string if unstructured."""
    if not path:
        return None
    parts = path.split(">")
    if len(parts) >= 2:
        hop = parts[-2]
        return hop.split(":", 1)[1] if ":" in hop else hop
    only = parts[0]
    return only.split(":", 1)[1] if ":" in only else only
