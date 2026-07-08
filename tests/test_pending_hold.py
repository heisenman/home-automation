"""Migration pending-hold: control_store accessors + viewmodel exclusion + the sweeper's fresh/expired/hold
resolution. (The device_push wiring is a thin call to store.set_pending; covered by these units.)"""
import sqlite3
from datetime import datetime, timezone

from server.api import viewmodel as V
from server.control import control_store as store
from server.maintenance import pending_sweeper as PS


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cc():
    c = sqlite3.connect(":memory:"); store.ensure_schema(c); return c


def _hot(device_id=None, last_ts=None):
    h = sqlite3.connect(":memory:")
    h.execute("CREATE TABLE device_last_seen (device_id TEXT PRIMARY KEY, last_ts TEXT)")
    h.execute("CREATE TABLE readings (device_id TEXT, ts TEXT)")
    if device_id and last_ts:
        h.execute("INSERT INTO device_last_seen VALUES (?,?)", (device_id, last_ts))
    h.commit(); return h


def test_set_get_clear_pending():
    c = _cc()
    until = store.set_pending(c, "d1", 6)
    assert store.get_device_meta(c, "d1")["pending_until"] == until
    store.clear_pending(c, "d1")
    assert store.get_device_meta(c, "d1")["pending_until"] is None


def test_pending_active_helper():
    now = 1000.0
    assert V._pending_active({"pending_until": _iso(now + 100)}, now) is True     # future → held
    assert V._pending_active({"pending_until": _iso(now - 100)}, now) is False     # past → not held
    assert V._pending_active({}, now) is False
    assert V._pending_active({"pending_until": "garbage"}, now) is False


def test_sweep_clears_hold_when_device_is_fresh():
    now = 2000.0
    c = _cc(); store.set_pending(c, "d1", 6)                                       # active hold
    h = _hot("d1", _iso(now - 60))                                                 # reported 1 min ago
    r = PS.sweep(c, h, now=now)
    assert r["cleared"] == 1 and store.get_device_meta(c, "d1")["pending_until"] is None


def test_sweep_drops_when_expired_and_stale_keeping_history():
    now = 2000.0
    c = _cc(); store.set_device_meta(c, "d1", pending_until=_iso(now - 100))       # already expired
    h = _hot("d1", _iso(now - 999999))                                             # ancient / no fresh data
    r = PS.sweep(c, h, now=now)
    m = store.get_device_meta(c, "d1")
    assert r["dropped"] == 1 and m["retired"] is True and m["pending_until"] is None   # hidden, history kept


def test_sweep_leaves_active_hold_alone():
    now = 2000.0
    c = _cc(); store.set_pending(c, "d1", 6)                                       # active, not expired
    h = _hot("d1", _iso(now - 999999))                                            # stale, but window open
    r = PS.sweep(c, h, now=now)
    assert r["kept"] == 1 and store.get_device_meta(c, "d1")["pending_until"] is not None
