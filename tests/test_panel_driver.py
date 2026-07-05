"""Tests for the wall-panel driver's pure logic: the trait→topic/payload map and the prefix→device_id
inversion (server/control/panel_driver.py), plus scene_brightness resolution (automation.scene_brightness).

NOTE: the controller-side scene-follower (`_apply_panel_scene` + the panel-skip in reconcile) was **backed
out 2026-07-05** — the panel backlight is a DEVICE-LOCAL control applied panel-side from /api/v1/house, not a
networked actuator on the control plane (ops d1001-panel-registration). `panel_driver.py` + `scene_brightness`
remain as reusable pieces (currently unwired); the MQTT transport needs paho + a broker, so it's exercised on
the box, not here.
"""
import tempfile
from pathlib import Path

from server.control import panel_driver as P
from server.control.automation import scene_brightness
from tests._harness import run_module


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


if __name__ == "__main__":
    run_module(globals())
