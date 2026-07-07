"""server/control/actuator_state.py — the shared actuator telemetry / area-stamping contract (ADR-0027).

ONE helper stamps every actuator's device_id + area, sourcing area from control.yaml (single source), so
Midea (controller) and Levoit (bridge) can't drift. Covers the pure helper + the Levoit single-source
resolution (control.yaml wins; levoit-devices.yaml area is the deprecated fallback).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.control import actuator_state as A  # noqa: E402


class _Ctl:
    def __init__(self, area):
        self.area = area


def test_resolve_area_deviceCtl_and_dict_and_missing():
    assert A.resolve_area({"d": _Ctl("living_room")}, "d") == "living_room"     # DeviceCtl attr
    assert A.resolve_area({"d": {"area": "kitchen"}}, "d") == "kitchen"          # dict shape
    assert A.resolve_area({}, "d", fallback="hall") == "hall"                    # missing -> fallback
    assert A.resolve_area({}, "d") == "unknown"                                  # missing, no fallback
    assert A.resolve_area(None, "d") == "unknown"                               # no registry


def test_actuator_state_payload_topic_and_merge():
    topic, p = A.actuator_state(device_id="dehumidifier_living_room", device_type="dehumidifier",
                                area="living_room", metrics={"humidity_pct": 40}, transport="midea-lan",
                                meta={"authoritative": False}, extra={"running": True, "target_pct": 45})
    assert topic == "home/living_room/dehumidifier_living_room/state"           # writer denormalization
    assert p["schema"] == 1 and p["device_id"] == "dehumidifier_living_room"
    assert p["device_type"] == "dehumidifier" and p["area"] == "living_room"
    assert p["transport"] == "midea-lan" and p["metrics"] == {"humidity_pct": 40}
    assert p["meta"] == {"authoritative": False}                                # family meta block
    assert p["running"] is True and p["target_pct"] == 45                       # family extras merged
    assert p["ts"].endswith("Z")                                               # fresh ts stamped


def test_actuator_state_area_in_payload_not_just_topic():
    # regression on writer.py:152 (reads area from payload, not topic)
    _, p = A.actuator_state(device_id="d", device_type="t", area="attic", metrics={}, transport="x")
    assert p["area"] == "attic"


def test_levoit_single_source_area_from_control_yaml():
    from server.ingest.levoit_bridge import LevoitBridge
    b = LevoitBridge({}, object())
    b._control_get = lambda: {"purifier_h_office": {"area": "living_room"}}
    # control.yaml wins even when levoit-devices.yaml still says something else (drift -> warn once)
    assert b._resolve_area("purifier_h_office", "office") == "living_room"
    assert b._resolve_area("purifier_h_office", "office") == "living_room"      # idempotent, warns once
    # device not declared in control.yaml -> deprecated fallback to the levoit area field
    assert b._resolve_area("purifier_unlisted", "attic") == "attic"
    # not in control.yaml and no levoit area -> unknown
    assert b._resolve_area("ghost", None) == "unknown"


def test_area_drift_guard(tmp_path=None):
    ctl = {"purifier_h_office": {"area": "living_room"}}
    # side registry still carrying a DISAGREEING area -> flagged (a re-introduced drift)
    assert A.area_drift(ctl, {"levoit-office": {"device_id": "purifier_h_office", "area": "office"}}) == \
        [("purifier_h_office", "office", "living_room")]
    # agreeing area -> no drift
    assert A.area_drift(ctl, {"levoit-office": {"device_id": "purifier_h_office", "area": "living_room"}}) == []
    # post-cutover: no area field at all -> no drift (the healthy state ADR-0027 step 4 leaves)
    assert A.area_drift(ctl, {"levoit-office": {"device_id": "purifier_h_office"}}) == []
    # device not declared in control.yaml -> not drift (deprecated fallback path, not a disagreement)
    assert A.area_drift(ctl, {"x": {"device_id": "ghost", "area": "attic"}}) == []


def test_levoit_absent_area_resolves_control_without_drift_warning():
    from server.ingest.levoit_bridge import LevoitBridge
    b = LevoitBridge({}, object())
    b._control_get = lambda: {"purifier_h_office": {"area": "living_room"}}
    # post-cutover the levoit area is None -> control.yaml wins, and NO drift warning fires
    assert b._resolve_area("purifier_h_office", None) == "living_room"
    assert "purifier_h_office" not in b._area_warned


if __name__ == "__main__":
    from tests._harness import run_module
    raise SystemExit(run_module(globals()))
