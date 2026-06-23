"""Tests for the ha-controller tick orchestration (server/control/controller.py)."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.control import controller as C, control_store as store  # noqa: E402
from server.control.issuer import Result  # noqa: E402
from server.control.midea_driver import MideaDriver  # noqa: E402
from tests._harness import run_module  # noqa: E402

NOW = 1_000_000.0
STATUS_ON = "  running = True\n  humid%  = 30\n  target% = 35\n  fan = 40\n  tank = False\n  error = 0\n"
STATUS_ON_TANK = STATUS_ON.replace("tank = False", "tank = True")


class FakeIssuer:
    def __init__(self):
        self.calls = []

    def issue(self, *, device_id, trait, action, args, **kw):
        self.calls.append({"device_id": device_id, "trait": trait, "args": args})
        return Result("ok", "ok", intended=args, reported=args)


class _Ctl:
    area = "living_room"


def _make(tmp, status_text):
    db = os.path.join(tmp, "control.db")
    conn = sqlite3.connect(db)
    store.ensure_schema(conn)
    store.seed_policy(conn, "dehumidifier_office", C.DEFAULT_POLICY)
    conn.close()
    drv = MideaDriver("ip", "t", "k", runner=lambda argv: status_text)
    iss = FakeIssuer()
    ctrl = C.Controller(iss, {"dehumidifier_office": drv}, {"dehumidifier_office": _Ctl()}, db)
    return ctrl, iss, db


def test_turns_off_when_room_below_threshold():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)              # device currently ON
        ctrl.inject_reading("meter_pro_living_room", 38.0, ts=NOW - 30)   # <40 -> rule wants OFF
        ctrl.tick(now=NOW)
        assert iss.calls and iss.calls[-1]["args"] == {"on": False}
        rows = store.recent_log(sqlite3.connect(db), "dehumidifier_office")
        assert rows[0]["source"] == "rule" and rows[0]["acted"] == 1 and rows[0]["status"] == "ok"


def test_dry_run_decides_but_does_not_issue():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)
        ctrl.inject_reading("meter_pro_living_room", 38.0, ts=NOW - 30)
        ctrl.tick(now=NOW, dry_run=True)
        assert iss.calls == []                              # nothing issued
        rows = store.recent_log(sqlite3.connect(db), "dehumidifier_office")
        assert rows[0]["acted"] == 1 and rows[0]["status"] == "dry-run"


def test_interlock_tank_full_forces_off():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON_TANK)         # device ON but tank full
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 30)   # would want ON
        ctrl.tick(now=NOW)
        assert iss.calls[-1]["args"] == {"on": False}
        rows = store.recent_log(sqlite3.connect(db), "dehumidifier_office")
        assert rows[0]["source"] == "safety"


class _FakeMqtt:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0):
        import json as _json
        self.published.append((topic, _json.loads(payload)))


def test_published_state_carries_timestamp():
    """Regression: onboard self-reports must carry a fresh ts, else the writer's (device_id,ts,metric)
    unique index collapses them and INSERT OR IGNORE freezes onboard RH forever."""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)
        ctrl.mqtt = _FakeMqtt()
        ctrl.inject_reading("meter_pro_living_room", 50.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        states = [p for t, p in ctrl.mqtt.published if t.endswith("/state")]
        assert states, "no state published"
        st = states[-1]
        assert st.get("ts"), f"state missing ts: {st}"
        assert st["metrics"].get("humidity_pct") == 30      # onboard value present...
        assert st["meta"]["authoritative"] is False          # ...flagged non-authoritative


def test_fallback_source_used_when_primary_stale():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)                  # device ON
        conn = sqlite3.connect(db)
        pol = store.get_policy(conn, "dehumidifier_office")
        pol["fallback_sensors"] = ["meter_backup"]
        store.set_policy(conn, "dehumidifier_office", pol)
        conn.close()
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 99999)  # primary STALE
        ctrl.inject_reading("meter_backup", 60.0, ts=NOW - 30)             # fallback FRESH, 60 -> ON
        ctrl.tick(now=NOW)
        # fallback fresh 60 >= 44 -> ON, device already ON -> no command. (Without fallback the stale
        # primary would default-OFF and issue {on:False}.) So no issue proves the fallback was used.
        assert iss.calls == []
        rows = store.recent_log(sqlite3.connect(db), "dehumidifier_office")
        assert "via fallback meter_backup" in rows[0]["reason"]


def test_disabled_policy_is_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)
        conn = sqlite3.connect(db)
        pol = store.get_policy(conn, "dehumidifier_office")
        pol["enabled"] = False
        store.set_policy(conn, "dehumidifier_office", pol)
        conn.close()
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        assert iss.calls == []                              # disabled -> no automation


if __name__ == "__main__":
    run_module(globals())
