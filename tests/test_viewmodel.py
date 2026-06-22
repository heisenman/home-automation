"""Tests for the display view-model BFF (server/api/viewmodel.py)."""
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.api import viewmodel as V  # noqa: E402
from server.control import control_store as store  # noqa: E402
from server.storage import writer as W  # noqa: E402
from tests._harness import run_module  # noqa: E402

DEV = "dehumidifier_office"
SRC = "meter_pro_living_room"
POLICY = {"enabled": True, "source_sensor": SRC,
          "control": {"strategy": "hysteresis", "on_above": 44, "off_below": 40},
          "sensor_stale_min": 10}


def _now_and_iso():
    t = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    return t.timestamp(), t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _control(tmp):
    c = sqlite3.connect(":memory:")
    store.ensure_schema(c)
    store.seed_policy(c, DEV, POLICY)
    return c


def _hot(tmp, src_ts, dehum_ts):
    conn = W._open_db(Path(tmp) / "hot.db")
    W._insert_readings(conn, {"schema": 1, "device_id": SRC, "device_type": "switchbot_meter",
                              "area": "living_room", "transport": "ble-adv", "ts": src_ts,
                              "metrics": {"humidity_pct": 43.0}})
    W._insert_readings(conn, {"schema": 1, "device_id": DEV, "device_type": "dehumidifier",
                              "area": "living_room", "transport": "midea-lan", "ts": dehum_ts,
                              "metrics": {"humidity_pct": 30.0}, "meta": {"authoritative": False}})
    return conn


def test_full_snapshot_ok():
    now, iso = _now_and_iso()
    with tempfile.TemporaryDirectory() as tmp:
        cc, hc = _control(tmp), _hot(tmp, iso, iso)
        store.append_log(cc, DEV, True, "rule", "RH 43 in deadband -> hold ON", False, "noop")
        vm = V.build_display(cc, hc, DEV, now + 30)            # 30s after the reading
        assert vm["running"] is True
        assert vm["health"] == "ok"
        assert vm["sensor"]["device_id"] == SRC and vm["sensor"]["humidity_pct"] == 43.0
        assert vm["sensor"]["age_s"] == 30
        assert vm["onboard"]["humidity_pct"] == 30.0          # device's own read, surfaced
        assert vm["control"]["on_above"] == 44
        assert vm["last_decision"]["source"] == "rule"
        assert vm["override"] is None


def test_stale_sensor_health():
    now, iso = _now_and_iso()
    with tempfile.TemporaryDirectory() as tmp:
        cc, hc = _control(tmp), _hot(tmp, iso, iso)
        vm = V.build_display(cc, hc, DEV, now + 11 * 60)       # 11m > 10m stale window
        assert vm["health"] == "stale"


def test_override_health_and_passthrough():
    now, iso = _now_and_iso()
    with tempfile.TemporaryDirectory() as tmp:
        cc, hc = _control(tmp), _hot(tmp, iso, iso)
        store.set_override(cc, DEV, "off", now + 20 * 60)
        vm = V.build_display(cc, hc, DEV, now + 30)
        assert vm["health"] == "overridden"
        assert vm["override"]["action"] == "off"


def test_disabled_health():
    now, iso = _now_and_iso()
    with tempfile.TemporaryDirectory() as tmp:
        cc, hc = _control(tmp), _hot(tmp, iso, iso)
        pol = store.get_policy(cc, DEV); pol["enabled"] = False
        store.set_policy(cc, DEV, pol)
        vm = V.build_display(cc, hc, DEV, now + 30)
        assert vm["health"] == "disabled"


def test_unknown_device_returns_none():
    now, _ = _now_and_iso()
    with tempfile.TemporaryDirectory() as tmp:
        cc = _control(tmp)
        assert V.build_display(cc, None, "nope", now) is None


def test_no_hot_db_degrades_gracefully():
    now, _ = _now_and_iso()
    with tempfile.TemporaryDirectory() as tmp:
        cc = _control(tmp)
        vm = V.build_display(cc, None, DEV, now)               # no hot.db connection
        assert vm["sensor"] is None and vm["onboard"] is None
        assert vm["health"] == "stale"                          # no sensor -> stale, fail-safe


if __name__ == "__main__":
    run_module(globals())
