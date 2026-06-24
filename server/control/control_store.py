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
-- device_meta: user-set OVERLAY on the registry (ADR-0014 R8) — friendly name, room, hidden flag.
-- The registry (devices.yaml/control.yaml) stays the source of truth; this just personalizes display.
CREATE TABLE IF NOT EXISTS device_meta (
    device_id TEXT PRIMARY KEY, name TEXT, room TEXT, hidden INTEGER NOT NULL DEFAULT 0, updated_ts TEXT);
-- device_calibration: per-(device, metric) DISPLAY offset (ADR-0014). Added to the value shown in the
-- UI + graphs; the control loop reads raw MQTT and is NOT affected (offset is display-only).
CREATE TABLE IF NOT EXISTS device_calibration (
    device_id TEXT, metric TEXT, offset REAL NOT NULL DEFAULT 0, PRIMARY KEY (device_id, metric));
-- push_subscription: Web Push endpoints (PWA web-push). Lives in control.db so it rides the existing
-- sync-standby snapshot -> subscriptions survive a dictator failover. p256dh/auth are kept for a future
-- payload push; the current payload-less tickle only needs `endpoint`.
CREATE TABLE IF NOT EXISTS push_subscription (
    endpoint TEXT PRIMARY KEY, p256dh TEXT, auth TEXT, created_ts TEXT NOT NULL);
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


# ── device meta (user overlay: friendly name / room / hidden) ─────────────────────
def get_device_meta(conn, device_id: str) -> dict | None:
    r = conn.execute("SELECT name, room, hidden FROM device_meta WHERE device_id=?",
                     (device_id,)).fetchone()
    return {"name": r[0], "room": r[1], "hidden": bool(r[2])} if r else None


def set_device_meta(conn, device_id: str, *, name=None, room=None, hidden=None) -> None:
    """Merge-update the overlay; a field left None keeps its current value (empty string clears a label)."""
    cur = get_device_meta(conn, device_id) or {"name": None, "room": None, "hidden": False}
    name = cur["name"] if name is None else name
    room = cur["room"] if room is None else room
    hidden = cur["hidden"] if hidden is None else hidden
    conn.execute("""INSERT INTO device_meta(device_id, name, room, hidden, updated_ts) VALUES(?,?,?,?,?)
                    ON CONFLICT(device_id) DO UPDATE SET name=excluded.name, room=excluded.room,
                        hidden=excluded.hidden, updated_ts=excluded.updated_ts""",
                 (device_id, name, room, int(bool(hidden)), _now_iso()))
    conn.commit()


def all_device_meta(conn) -> dict:
    return {r[0]: {"name": r[1], "room": r[2], "hidden": bool(r[3])}
            for r in conn.execute("SELECT device_id, name, room, hidden FROM device_meta")}


# ── display calibration (per device+metric offset; display-only) ─────────────────
def set_calibration(conn, device_id: str, metric: str, offset: float) -> None:
    if not offset:                                          # 0 clears the offset
        conn.execute("DELETE FROM device_calibration WHERE device_id=? AND metric=?", (device_id, metric))
    else:
        conn.execute("""INSERT INTO device_calibration(device_id, metric, offset) VALUES(?,?,?)
                        ON CONFLICT(device_id, metric) DO UPDATE SET offset=excluded.offset""",
                     (device_id, metric, float(offset)))
    conn.commit()


def all_calibration(conn) -> dict:
    out: dict = {}
    for d, m, o in conn.execute("SELECT device_id, metric, offset FROM device_calibration"):
        out.setdefault(d, {})[m] = o
    return out


# ── Web Push subscriptions (PWA web-push) ────────────────────────────────────────
def add_push_sub(conn, endpoint: str, p256dh: str = "", auth: str = "") -> None:
    """Idempotent on endpoint (re-subscribing the same browser is a no-op refresh)."""
    conn.execute(
        """INSERT INTO push_subscription (endpoint, p256dh, auth, created_ts) VALUES (?,?,?,?)
           ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth""",
        (endpoint, p256dh, auth, _now_iso()))
    conn.commit()


def remove_push_sub(conn, endpoint: str) -> None:
    conn.execute("DELETE FROM push_subscription WHERE endpoint=?", (endpoint,))
    conn.commit()


def all_push_subs(conn) -> list[dict]:
    return [{"endpoint": e, "p256dh": p, "auth": a}
            for e, p, a in conn.execute("SELECT endpoint, p256dh, auth FROM push_subscription")]
