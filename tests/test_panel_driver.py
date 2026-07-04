"""Tests for the wall-panel actuator: scene-brightness resolution, the panel driver's paho-free logic,
and the controller scene-follower (server/control/panel_driver.py + automation.scene_brightness +
controller._apply_panel_scene). The MQTT transport itself needs paho + a broker, so it is exercised on
the box, not here — these guard the pure logic and the scene→brightness wiring (D1001 roadmap #2)."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.control import controller as C, control_store as store  # noqa: E402
from server.control import panel_driver as P  # noqa: E402
from server.control.automation import scene_brightness  # noqa: E402
from server.control.issuer import Result  # noqa: E402
from tests._harness import run_module  # noqa: E402

NOW = 1_000_000.0


# ── scene_brightness (pure) ───────────────────────────────────────────────────────
def test_scene_brightness_patch_wins_over_base():
    pol = {"brightness": 100, "scenes": {"Sleep": {"brightness": 0}, "Away": {"brightness": 40}}}
    assert scene_brightness(pol, "Sleep") == 0        # scene patch
    assert scene_brightness(pol, "Away") == 40
    assert scene_brightness(pol, "Home") == 100       # no patch -> base
    assert scene_brightness({}, "Home") is None       # no opinion -> leave alone


# ── panel driver mapping (paho-free bits) ─────────────────────────────────────────
def test_trait_map_topics_and_payloads():
    sw = P._TRAIT_MAP["switchable"]
    sp = P._TRAIT_MAP["setpoint"]
    assert sw[1] == "cmd/screen" and sw[2](True) == "on" and sw[2](False) == "off"
    assert sp[1] == "cmd/brightness" and sp[2](40) == "40" and sp[2](0) == "0"
    # both read the single retained JSON state topic, extracting their own field
    assert sw[3] == "state/screen" and sp[3] == "state/screen"
    assert sw[4]('{"on": true, "level": 40}') is True
    assert sp[4]('{"on": true, "level": 40}') == 40
    assert sp[4]('{"on": false, "level": 0}') == 0


def test_load_panel_devices_inverts_prefix_to_device_id():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "panel-devices.yaml"
        f.write_text("d1001-beachhead:\n  device_id: d1001_panel\n")
        assert P.load_panel_devices(f) == {"d1001_panel": "d1001-beachhead"}
        # default: dashes in the prefix -> underscores in the device_id
        f.write_text("e1001-bench: {}\n")
        assert P.load_panel_devices(f) == {"e1001_bench": "e1001-bench"}
        assert P.load_panel_devices(Path(tmp) / "nope.yaml") == {}


# ── controller scene-follower ─────────────────────────────────────────────────────
class _FakeIssuer:
    def __init__(self):
        self.calls = []

    def issue(self, *, device_id, trait, action, args, **kw):
        self.calls.append({"device_id": device_id, "trait": trait, "args": args})
        return Result("ok", "ok", intended=args, reported=args)


class _PanelCtl:
    area = "office"
    device_type = "panel"


def _make(tmp, scenes):
    db = os.path.join(tmp, "control.db")
    conn = sqlite3.connect(db)
    store.ensure_schema(conn)
    store.seed_policy(conn, "d1001_panel", {"enabled": True, "brightness": 100, "scenes": scenes})
    conn.close()
    iss = _FakeIssuer()
    ctrl = C.Controller(iss, {}, {"d1001_panel": _PanelCtl()}, db)
    return ctrl, iss, db


def test_scene_change_drives_panel_setpoint():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, {"Sleep": {"brightness": 0}, "Away": {"brightness": 40}})
        conn = sqlite3.connect(db); store.set_scene(conn, "Sleep"); conn.close()
        ctrl.tick(now=NOW)
        assert iss.calls == [{"device_id": "d1001_panel", "trait": "setpoint", "args": {"value": 0}}]
        rows = store.recent_log(sqlite3.connect(db), "d1001_panel")
        assert rows[0]["source"] == "scene" and rows[0]["acted"] == 1


def test_scene_follower_is_edge_triggered():
    # same scene across ticks -> issue once, not every tick (a manual PWA brightness set is left alone)
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, {"Away": {"brightness": 40}})
        conn = sqlite3.connect(db); store.set_scene(conn, "Away"); conn.close()
        ctrl.tick(now=NOW)
        ctrl.tick(now=NOW + 45)
        ctrl.tick(now=NOW + 90)
        assert len(iss.calls) == 1 and iss.calls[0]["args"] == {"value": 40}


def test_panel_skipped_by_sensor_reconcile():
    # a panel has a policy but must NOT go through the closed-loop _tick_device (no driver/telemetry);
    # only the scene-follower touches it. With no scene set (Home) and base brightness, Home change fires once.
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, {})
        ctrl.tick(now=NOW)                       # scene defaults to Home -> base brightness 100
        assert iss.calls == [{"device_id": "d1001_panel", "trait": "setpoint", "args": {"value": 100}}]
        # no "no telemetry yet" style sensor-loop log for the panel
        rows = store.recent_log(sqlite3.connect(db), "d1001_panel")
        assert all(r["source"] == "scene" for r in rows), rows


def test_scene_dry_run_does_not_issue():
    with tempfile.TemporaryDirectory() as tmp:
        ctrl, iss, db = _make(tmp, {"Sleep": {"brightness": 0}})
        conn = sqlite3.connect(db); store.set_scene(conn, "Sleep"); conn.close()
        ctrl.tick(now=NOW, dry_run=True)
        assert iss.calls == []


if __name__ == "__main__":
    run_module(globals())
