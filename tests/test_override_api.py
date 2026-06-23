"""Tests for the manual-override + control-state API (server/api/control.py)."""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.api import control as C  # noqa: E402
from server.control import control_store as store  # noqa: E402
from tests._harness import run_module  # noqa: E402

NOW = 1_000_000.0
DEV = "dehumidifier_office"
POLICY = {"enabled": True, "source_sensor": "meter_pro_living_room",
          "control": {"strategy": "hysteresis", "on_above": 44, "off_below": 40}}


def _conn():
    c = sqlite3.connect(":memory:")
    store.ensure_schema(c)
    store.seed_policy(c, DEV, POLICY)
    return c


def test_set_off_override_writes_ttl():
    c = _conn()
    code, body = C.handle_override(c, DEV, {"action": "off", "duration_min": 30}, NOW, {DEV})
    assert code == 200 and body["override"]["action"] == "off", body
    assert body["override"]["expiry"] == NOW + 30 * 60
    # the controller reads it as ACTIVE
    assert store.get_override(c, DEV, NOW) == ("off", NOW + 1800)


def test_boost_on_override():
    c = _conn()
    code, body = C.handle_override(c, DEV, {"action": "boost_on", "duration_min": 15}, NOW, {DEV})
    assert code == 200 and store.get_override(c, DEV, NOW)[0] == "boost_on", body


def test_clear_override():
    c = _conn()
    C.handle_override(c, DEV, {"action": "off", "duration_min": 30}, NOW, {DEV})
    code, body = C.handle_override(c, DEV, {"action": "clear"}, NOW, {DEV})
    assert code == 200 and body["override"] is None
    assert store.get_override(c, DEV, NOW) is None


def test_bad_action_and_duration():
    c = _conn()
    assert C.handle_override(c, DEV, {"action": "frobnicate"}, NOW, {DEV})[0] == 400
    assert C.handle_override(c, DEV, {"action": "off"}, NOW, {DEV})[0] == 400          # no duration
    assert C.handle_override(c, DEV, {"action": "off", "duration_min": -5}, NOW, {DEV})[0] == 400
    assert C.handle_override(c, DEV, {"action": "off", "duration_min": 99999}, NOW, {DEV})[0] == 400
    assert C.handle_override(c, DEV, {"action": "off", "duration_min": True}, NOW, {DEV})[0] == 400


def test_unknown_device():
    c = _conn()
    code, body = C.handle_override(c, "nope", {"action": "clear"}, NOW, {DEV})
    assert code == 404 and body["status"] == "unknown-device"


def test_expired_override_is_inactive():
    c = _conn()
    C.handle_override(c, DEV, {"action": "off", "duration_min": 10}, NOW, {DEV})
    assert store.get_override(c, DEV, NOW + 11 * 60) is None        # 10m TTL elapsed -> auto-clears


def test_read_control_state_snapshot():
    c = _conn()
    C.handle_override(c, DEV, {"action": "boost_on", "duration_min": 20}, NOW, {DEV})
    store.append_log(c, DEV, True, "override", "override BOOST-ON", True, "ok")
    snap = C.read_control_state(c, DEV, NOW)
    assert snap["device_id"] == DEV
    assert snap["policy"]["source_sensor"] == "meter_pro_living_room"
    assert snap["override"]["action"] == "boost_on"
    assert 19 < snap["override"]["expires_in_min"] <= 20
    assert snap["last_decision"]["source"] == "override"


def test_policy_merge_patch_thresholds():
    c = _conn()
    code, body = C.handle_policy_update(c, DEV, {"control": {"on_above": 50, "off_below": 45}}, {DEV})
    assert code == 200, body
    pol = store.get_policy(c, DEV)
    assert pol["control"]["on_above"] == 50 and pol["control"]["off_below"] == 45
    assert pol["control"]["strategy"] == "hysteresis"           # untouched field preserved
    assert pol["source_sensor"] == "meter_pro_living_room"      # merge, not replace


def test_policy_enable_toggle_and_schedule():
    c = _conn()
    code, _ = C.handle_policy_update(c, DEV, {"enabled": False,
                                              "schedule": [{"when": "22:00-07:00", "policy": "off"}]}, {DEV})
    assert code == 200
    pol = store.get_policy(c, DEV)
    assert pol["enabled"] is False and pol["schedule"][0]["when"] == "22:00-07:00"


def test_policy_set_source_sensor():
    c = _conn()
    code, _ = C.handle_policy_update(c, DEV, {"source_sensor": "meter_pro_c_office"}, {DEV})
    assert code == 200
    assert store.get_policy(c, DEV)["source_sensor"] == "meter_pro_c_office"
    # empty/blank source rejected (would strand the loop with no input)
    assert C.handle_policy_update(c, DEV, {"source_sensor": ""}, {DEV})[0] == 400


def test_policy_fallback_sensors():
    c = _conn()
    code, _ = C.handle_policy_update(c, DEV, {"fallback_sensors": ["meter_a", "meter_b"]}, {DEV})
    assert code == 200 and store.get_policy(c, DEV)["fallback_sensors"] == ["meter_a", "meter_b"]
    assert C.handle_policy_update(c, DEV, {"fallback_sensors": "nope"}, {DEV})[0] == 400
    assert C.handle_policy_update(c, DEV, {"fallback_sensors": [""]}, {DEV})[0] == 400


def test_policy_rejects_inverted_deadband():
    c = _conn()
    code, body = C.handle_policy_update(c, DEV, {"control": {"on_above": 40, "off_below": 44}}, {DEV})
    assert code == 400 and "deadband" in body["reason"], body


def test_policy_rejects_bad_strategy_and_schedule():
    c = _conn()
    assert C.handle_policy_update(c, DEV, {"control": {"strategy": "magic"}}, {DEV})[0] == 400
    assert C.handle_policy_update(c, DEV, {"schedule": [{"when": "nope", "policy": "off"}]}, {DEV})[0] == 400
    assert C.handle_policy_update(c, DEV, {"enabled": "yes"}, {DEV})[0] == 400
    assert C.handle_policy_update(c, "nope", {"enabled": True}, {DEV})[0] == 404


def test_device_meta_set_merge_and_validate():
    c = _conn()
    # set name + room
    code, body = C.handle_device_meta(c, "meter_attic", {"name": "Attic", "room": "attic loft"})
    assert code == 200 and body["meta"]["name"] == "Attic" and body["meta"]["room"] == "attic loft"
    # merge: hide without touching name/room
    C.handle_device_meta(c, "meter_attic", {"hidden": True})
    m = store.get_device_meta(c, "meter_attic")
    assert m["hidden"] is True and m["name"] == "Attic"      # name preserved across merge
    # empty string clears a label
    C.handle_device_meta(c, "meter_attic", {"name": ""})
    assert store.get_device_meta(c, "meter_attic")["name"] == ""
    # validation
    assert C.handle_device_meta(c, "meter_attic", {"hidden": "yes"})[0] == 400
    assert C.handle_device_meta(c, "meter_attic", {})[0] == 400
    assert C.handle_device_meta(c, "meter_attic", {"name": 5})[0] == 400


def test_device_calibration():
    c = _conn()
    code, body = C.handle_device_calibration(c, "meter_x", {"metric": "humidity_pct", "offset": -2.5})
    assert code == 200 and store.all_calibration(c)["meter_x"]["humidity_pct"] == -2.5
    C.handle_device_calibration(c, "meter_x", {"metric": "humidity_pct", "offset": 0})   # 0 clears
    assert "meter_x" not in store.all_calibration(c)
    assert C.handle_device_calibration(c, "meter_x", {"metric": "", "offset": 1})[0] == 400
    assert C.handle_device_calibration(c, "meter_x", {"metric": "humidity_pct", "offset": "x"})[0] == 400


def test_router_requires_admin_bearer():
    """End-to-end: override + control-state routes 401 without the SHA bearer, 200 with it."""
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except Exception:
        print("    (skip: fastapi/testclient not available)")
        return
    from server.control import secret_store as S
    master = "CHANGE_ME_master_passphrase"
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "control.db"
        cc = sqlite3.connect(str(db))
        store.ensure_schema(cc)
        store.seed_policy(cc, DEV, POLICY)
        cc.close()
        app = FastAPI()
        app.include_router(C.make_override_router(S.make_api_token_verifier(master), db,
                                                  device_ids={DEV}))
        client = TestClient(app)
        body = {"action": "off", "duration_min": 30}
        assert client.post(f"/control/{DEV}/override", json=body).status_code == 401
        assert client.get(f"/control/{DEV}").status_code == 401
        hdr = {"Authorization": f"Bearer {S.api_token(master)}"}
        r = client.post(f"/control/{DEV}/override", json=body, headers=hdr)
        assert r.status_code == 200 and r.json()["override"]["action"] == "off", (r.status_code, r.text)
        g = client.get(f"/control/{DEV}", headers=hdr)
        assert g.status_code == 200 and g.json()["override"]["action"] == "off", g.text
        assert client.get("/control/nope", headers=hdr).status_code == 404


if __name__ == "__main__":
    run_module(globals())
