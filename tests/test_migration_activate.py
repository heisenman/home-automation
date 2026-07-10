"""Unit tests for tools/migration_activate.py — the post-migration ACTIVATE assertion.

Exercises the pure core (check_device / sweep) over in-memory fixtures: a fabricated hot.db with
device_last_seen + readings, a registered-id map, and a fake quarantine store. Covers every verdict the
live smoke test couldn't fabricate (stale, registered-but-silent, quarantine victim surfacing).
"""
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools.migration_activate import check_device, sweep  # noqa: E402

NOW = 1_000_000.0


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _hot(seen=None, readings=None) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE device_last_seen (device_id TEXT PRIMARY KEY, last_ts TEXT)")
    c.execute("CREATE TABLE readings (device_id TEXT, ts TEXT)")
    for did, age in (seen or {}).items():
        c.execute("INSERT INTO device_last_seen VALUES (?,?)", (did, _iso(NOW - age)))
    for did, age in (readings or {}).items():
        c.execute("INSERT INTO readings VALUES (?,?)", (did, _iso(NOW - age)))
    c.commit()
    return c


class _FakeQ:
    def __init__(self, devices): self._d = devices
    def list_devices(self, **_): return self._d


def test_activated():
    hot = _hot(seen={"pm_1": 70})
    rep = check_device("pm_1", hot, {"pm_1": "tasmota-devices.yaml"}, None, now=NOW, max_age=900)
    assert rep["verdict"] == "ACTIVATED"
    assert rep["registered"]["ok"] and rep["logging"]["ok"]


def test_not_registered_is_the_silent_drop_signature():
    hot = _hot(seen={})
    rep = check_device("pm_1", hot, {}, None, now=NOW, max_age=900)
    assert rep["verdict"] == "NOT_ACTIVATED"
    assert rep["registered"]["ok"] is False
    assert "NOT in any destination registry" in rep["reason"]


def test_registered_but_no_data():
    hot = _hot(seen={})
    rep = check_device("pm_1", hot, {"pm_1": "tasmota-devices.yaml"}, None, now=NOW, max_age=900)
    assert rep["verdict"] == "NOT_ACTIVATED"
    assert "NO data" in rep["reason"]


def test_registered_but_stale():
    hot = _hot(seen={"pm_1": 5000})   # > max_age
    rep = check_device("pm_1", hot, {"pm_1": "tasmota-devices.yaml"}, None, now=NOW, max_age=900)
    assert rep["verdict"] == "NOT_ACTIVATED"
    assert "STALE" in rep["reason"]


def test_readings_fallback_when_no_last_seen():
    hot = _hot(seen={}, readings={"pm_1": 100})   # only readings, no device_last_seen row
    rep = check_device("pm_1", hot, {"pm_1": "tasmota-devices.yaml"}, None, now=NOW, max_age=900)
    assert rep["verdict"] == "ACTIVATED"


def test_quarantine_surfaced_when_not_logging():
    hot = _hot(seen={})
    q = _FakeQ([{"source": "tasmota", "identity": "airgap_router_pm", "sample_count": 42, "last_seen": "x"}])
    rep = check_device("airgap_router_pm", hot, {}, q, now=NOW, max_age=900)
    assert rep["verdict"] == "NOT_ACTIVATED"
    hit = rep["quarantine"]["live_but_unregistered"][0]
    assert hit["identity"] == "airgap_router_pm" and hit["likely_this_device"] is True


def test_sweep_flags_quarantine_and_fresh_unregistered():
    hot = _hot(seen={"ghost_dev": 60})   # fresh but not in regmap -> belt-and-suspenders catch
    q = _FakeQ([{"source": "edge", "identity": "E1001-C-OFFICE-SHT40", "sample_count": 9,
                 "first_seen": "a", "last_seen": "b", "device_type_hint": None}])
    rep = sweep(hot, regmap={}, qstore=q, now=NOW)
    assert rep["count"] == 2
    assert rep["quarantine_victims"][0]["identity"] == "E1001-C-OFFICE-SHT40"
    assert rep["fresh_but_unregistered"][0]["device_id"] == "ghost_dev"


def test_sweep_clean():
    hot = _hot(seen={"pm_1": 60})
    rep = sweep(hot, regmap={"pm_1": "tasmota-devices.yaml"}, qstore=_FakeQ([]), now=NOW)
    assert rep["count"] == 0
