"""Controller state store — instance/db/control.db (ADR-0011).

Holds the runtime state the ha-controller owns, kept SEPARATE from sensor data so the compactor never
touches it and the web app can edit policy live:
  - automation_policy : per-device control policy (source sensor, thresholds, schedule, enabled) — the
                        app-mutable settings; the controller reads it every tick so edits take effect.
  - cycle_state       : last on/off timestamps per device (compressor min-on/min-off protection).
  - override          : manual TTL override (off | boost_on | clear) + expiry.
  - control_log       : audited decision trail (why is it on?).

Thin I/O over sqlite; the decision logic is the pure resolver (automation.py).
"""
from __future__ import annotations

import json
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS automation_policy (
    device_id TEXT PRIMARY KEY, json TEXT NOT NULL, updated_ts TEXT);
CREATE TABLE IF NOT EXISTS cycle_state (
    device_id TEXT PRIMARY KEY, last_on_ts REAL, last_off_ts REAL);
CREATE TABLE IF NOT EXISTS override (
    device_id TEXT PRIMARY KEY, action TEXT NOT NULL, expiry REAL);
CREATE TABLE IF NOT EXISTS control_log (
    ts TEXT, device_id TEXT, desired INTEGER, source TEXT, reason TEXT, acted INTEGER, status TEXT);
CREATE INDEX IF NOT EXISTS idx_control_log ON control_log(device_id, ts);
"""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


# ── automation policy (app-mutable) ──────────────────────────────────────────────
def get_policy(conn, device_id: str) -> dict | None:
    r = conn.execute("SELECT json FROM automation_policy WHERE device_id=?", (device_id,)).fetchone()
    return json.loads(r[0]) if r else None


def set_policy(conn, device_id: str, policy: dict) -> None:
    conn.execute("""INSERT INTO automation_policy(device_id, json, updated_ts) VALUES(?,?,?)
                    ON CONFLICT(device_id) DO UPDATE SET json=excluded.json, updated_ts=excluded.updated_ts""",
                 (device_id, json.dumps(policy), _now_iso()))
    conn.commit()


def seed_policy(conn, device_id: str, policy: dict) -> None:
    """Set the policy only if none exists yet (first-run defaults; app edits win thereafter)."""
    if get_policy(conn, device_id) is None:
        set_policy(conn, device_id, policy)


def all_policies(conn) -> dict:
    return {r[0]: json.loads(r[1]) for r in conn.execute("SELECT device_id, json FROM automation_policy")}


# ── compressor cycle state ───────────────────────────────────────────────────────
def get_cycle(conn, device_id: str):
    r = conn.execute("SELECT last_on_ts, last_off_ts FROM cycle_state WHERE device_id=?",
                     (device_id,)).fetchone()
    return (r[0], r[1]) if r else (None, None)


def record_transition(conn, device_id: str, running: bool, ts: float) -> None:
    cur_on, cur_off = get_cycle(conn, device_id)
    new_on = ts if running else cur_on
    new_off = ts if not running else cur_off
    conn.execute("""INSERT INTO cycle_state(device_id, last_on_ts, last_off_ts) VALUES(?,?,?)
                    ON CONFLICT(device_id) DO UPDATE SET last_on_ts=excluded.last_on_ts,
                                                         last_off_ts=excluded.last_off_ts""",
                 (device_id, new_on, new_off))
    conn.commit()


# ── manual override (TTL) ────────────────────────────────────────────────────────
def get_override(conn, device_id: str, now: float | None = None):
    """Returns (action, expiry) for an ACTIVE override, else None (cleared or expired)."""
    r = conn.execute("SELECT action, expiry FROM override WHERE device_id=?", (device_id,)).fetchone()
    if not r:
        return None
    action, expiry = r
    if action == "clear":
        return None
    if expiry is not None and now is not None and expiry <= now:
        return None
    return (action, expiry)


def set_override(conn, device_id: str, action: str, expiry: float | None) -> None:
    conn.execute("""INSERT INTO override(device_id, action, expiry) VALUES(?,?,?)
                    ON CONFLICT(device_id) DO UPDATE SET action=excluded.action, expiry=excluded.expiry""",
                 (device_id, action, expiry))
    conn.commit()


def clear_override(conn, device_id: str) -> None:
    conn.execute("DELETE FROM override WHERE device_id=?", (device_id,))
    conn.commit()


# ── control log ──────────────────────────────────────────────────────────────────
def append_log(conn, device_id: str, desired: bool, source: str, reason: str,
               acted: bool, status: str) -> None:
    conn.execute("""INSERT INTO control_log(ts, device_id, desired, source, reason, acted, status)
                    VALUES(?,?,?,?,?,?,?)""",
                 (_now_iso(), device_id, int(bool(desired)), source, reason, int(bool(acted)), status))
    conn.commit()


def recent_log(conn, device_id: str, limit: int = 50) -> list[dict]:
    cols = ("ts", "desired", "source", "reason", "acted", "status")
    return [dict(zip(cols, r)) for r in conn.execute(
        f"SELECT {','.join(cols)} FROM control_log WHERE device_id=? ORDER BY ts DESC LIMIT ?",
        (device_id, limit))]
