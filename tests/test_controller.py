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


def test_published_state_carries_area():
    """Regression: the writer reads area from the PAYLOAD (writer.py: payload.get('area','unknown')),
    not the topic — so a missing area field stamps device_last_seen.area='unknown' and the device drops
    off the map. The self-report payload must include the registry area."""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)
        ctrl.mqtt = _FakeMqtt()
        ctrl.inject_reading("meter_pro_living_room", 50.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        topic, st = next((t, p) for t, p in ctrl.mqtt.published if t.endswith("/state"))
        assert st.get("area") == "living_room", f"state missing area: {st}"
        assert topic == "home/living_room/dehumidifier_office/state"   # topic + payload agree


class _CtlAt:
    def __init__(self, area):
        self.area = area


class _FakeReloader:
    """Stands in for RegistryReloader — .current() returns whatever the (simulated) control.yaml now holds."""
    def __init__(self, registry):
        self._registry = registry

    def current(self):
        return self._registry


def test_actuator_relocate_reloads_area_without_restart():
    """controller-area-reload: relocating an actuator edits its area in control.yaml. tick() must pick up
    the new area live (via the attached reloader) so the self-report stamps the NEW area — no restart, no
    pop-back. The controller is the 6th area-stamper, sibling of the ingest bridges' devices.yaml reload."""
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)
        ctrl.mqtt = _FakeMqtt()
        ctrl.inject_reading("meter_pro_living_room", 50.0, ts=NOW - 30)
        # simulate a relocate: control.yaml now says h_office for this actuator
        ctrl.attach_registry_reloader(_FakeReloader({"dehumidifier_office": _CtlAt("h_office")}))
        ctrl.tick(now=NOW)
        topic, st = next((t, p) for t, p in ctrl.mqtt.published if t.endswith("/state"))
        assert st.get("area") == "h_office", f"reloaded area not applied: {st}"
        assert topic == "home/h_office/dehumidifier_office/state"


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


def test_sleep_scene_parks_a_running_device():
    # device ON + RH 60 (rule would keep it ON), but Sleep scene forces OFF
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON)
        conn = sqlite3.connect(db)
        pol = store.get_policy(conn, "dehumidifier_office")
        pol["scenes"] = {"Sleep": {"off": True}}
        store.set_policy(conn, "dehumidifier_office", pol)
        store.set_scene(conn, "Sleep")
        conn.close()
        ctrl.inject_reading("meter_pro_living_room", 60.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        assert iss.calls and iss.calls[-1]["args"] == {"on": False}
        rows = store.recent_log(sqlite3.connect(db), "dehumidifier_office")
        assert rows[0]["source"] == "scene", rows[0]


def test_away_scene_relaxes_thresholds():
    # RH 50: ON under Home (>=44), but Away relaxes to on_above 60 -> stays OFF (deadband/below)
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, STATUS_ON.replace("running = True", "running = False"))
        conn = sqlite3.connect(db)
        pol = store.get_policy(conn, "dehumidifier_office")
        pol["scenes"] = {"Away": {"on_above": 60, "off_below": 55}}
        store.set_policy(conn, "dehumidifier_office", pol)
        store.set_scene(conn, "Away")
        conn.close()
        ctrl.inject_reading("meter_pro_living_room", 50.0, ts=NOW - 30)
        ctrl.tick(now=NOW)
        assert iss.calls == []                              # relaxed -> no turn-on at RH 50


if __name__ == "__main__":
    run_module(globals())
