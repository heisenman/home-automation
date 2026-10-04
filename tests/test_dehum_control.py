"""Aprilaire automation + ERV coupling (Hugh, 2026-10-04): averaged house RH drives a LEASED DH call; the
dehumidifier puts a FLOOR on the ERV (during + coil dry-out), never a ceiling; an operator ERV-off wins."""
import json
import sqlite3

import pytest

from server.control import control_store as store
from server.control import controller as C
from server.control.issuer import Result
from server.control.registry import parse_control_registry

yaml = pytest.importorskip("yaml")
NOW = 2_000_000.0


def _reg():
    return parse_control_registry(yaml.safe_load(open("config-examples/control.example.yaml")))


class Iss:
    def __init__(self):
        self.calls = []

    def issue(self, *, device_id, trait, action, args, **kw):
        self.calls.append((device_id, trait, args))
        return Result("ok", "ok", intended=args, reported=args)


class _Msg:
    def __init__(self, did, metrics):
        self.payload = json.dumps({"device_id": did, "metrics": metrics}).encode()
        self.topic = f"home/attic/{did}/state"


def _make(tmp_path, *, erv_enabled=True, rh=(60, 62), dh_running=False, lease_left=0, erv_mode=9):
    db = str(tmp_path / "control.db")
    conn = sqlite3.connect(db)
    store.ensure_schema(conn)
    store.set_policy(conn, "erv_attic", {**C.ERV_POLICY, "enabled": erv_enabled, "source_sensors": ["gas_a"]})
    store.set_policy(conn, "dehum_attic", {**C.DEHUM_POLICY, "enabled": True,
                                           "source_sensors": ["rh_a", "rh_b"],
                                           "ventilation": {**C.DEHUM_POLICY["ventilation"],
                                                           "device": "erv_attic"}})
    conn.close()
    iss = Iss()
    ctrl = C.Controller(iss, {}, _reg(), db)
    ctrl._latest_stored = lambda sid, metric, n: {"m": {metric: 90.0}, "ts": n - 60}   # ERV AQ: clean -> low
    for sid, v in zip(("rh_a", "rh_b"), rh):
        ctrl.inject_reading(sid, v, ts=NOW - 30)
    ctrl.on_message(None, None, _Msg("erv_attic", {"fan_mode": erv_mode}))
    ctrl.on_message(None, None, _Msg("dehum_attic", {"dh_call": dh_running, "call_left_s": lease_left}))
    return ctrl, iss, db


def _erv_cmds(iss):
    return [a for d, t, a in iss.calls if d == "erv_attic"]


def test_humid_house_calls_dehum_and_floors_the_erv_at_med(tmp_path):
    ctrl, iss, _ = _make(tmp_path, rh=(60, 62))                    # mean 61 >= 55
    ctrl.tick(now=NOW)
    assert ("dehum_attic", "switchable", {"on": True}) in iss.calls
    assert _erv_cmds(iss)[-1] == {"mode": "med"}                    # own level = low; floor lifts to med


def test_floor_never_lowers_the_erv(tmp_path):
    ctrl, iss, _ = _make(tmp_path, rh=(60, 62), erv_mode=12)        # already turbo
    ctrl._latest_stored = lambda sid, metric, n: {"m": {metric: 10.0}, "ts": n - 60}   # bad air -> turbo
    ctrl.tick(now=NOW)
    assert _erv_cmds(iss) == []                                      # stays turbo, no downward command


def test_running_lease_is_renewed_before_it_runs_out(tmp_path):
    ctrl, iss, _ = _make(tmp_path, rh=(60, 62), dh_running=True, lease_left=200, erv_mode=11)
    ctrl.tick(now=NOW)
    assert ("dehum_attic", "switchable", {"on": True}) in iss.calls


def test_stop_starts_a_high_dry_out_that_then_ends(tmp_path):
    ctrl, iss, db = _make(tmp_path, rh=(60, 62), dh_running=True, lease_left=500, erv_mode=11)
    ctrl.tick(now=NOW)                                               # running: floor med (already med)
    # house dried out, min-on long past
    for sid in ("rh_a", "rh_b"):
        ctrl.inject_reading(sid, 45, ts=NOW + 3600 - 30)
    ctrl.tick(now=NOW + 3600)
    assert ("dehum_attic", "switchable", {"on": False}) in iss.calls
    assert _erv_cmds(iss)[-1] == {"mode": "high"}                   # coil dry-out
    ctrl.on_message(None, None, _Msg("dehum_attic", {"dh_call": False, "call_left_s": 0}))
    ctrl.on_message(None, None, _Msg("erv_attic", {"fan_mode": 10}))
    for sid in ("rh_a", "rh_b"):
        ctrl.inject_reading(sid, 45, ts=NOW + 3600 + 16 * 60 - 30)
    ctrl.tick(now=NOW + 3600 + 16 * 60)                              # 15 min window over
    assert _erv_cmds(iss)[-1] == {"mode": "low"}                    # back to its own air-quality level


def test_dehum_holds_when_the_erv_is_off_and_cannot_be_raised(tmp_path):
    ctrl, iss, _ = _make(tmp_path, rh=(60, 62), erv_enabled=False, erv_mode=1)
    ctrl.tick(now=NOW)
    assert ("dehum_attic", "switchable", {"on": True}) not in iss.calls


def test_operator_erv_off_wins_over_the_floor(tmp_path):
    ctrl, iss, db = _make(tmp_path, rh=(60, 62), erv_mode=1)
    conn = sqlite3.connect(db)
    store.set_override(conn, "erv_attic", "off", NOW + 3600)
    conn.close()
    ctrl.tick(now=NOW)
    assert ("dehum_attic", "switchable", {"on": True}) not in iss.calls   # interlock: no airflow
    assert {"mode": "med"} not in _erv_cmds(iss)


def test_called_but_not_running_alarm(tmp_path):
    from pathlib import Path

    from server.api.viewmodel import verify_power_alert
    from server.storage import writer as W
    hc = W._open_db(Path(tmp_path) / "hot.db")
    for ts, call in (("2026-10-04T22:00:00Z", 0), ("2026-10-04T22:01:00Z", 1), ("2026-10-04T22:09:00Z", 1)):
        W._insert_readings(hc, {"schema": 1, "device_id": "dehum_attic", "device_type": "dehum", "area": "attic",
                                "transport": "gpio", "ts": ts, "metrics": {"dh_call": call}})
    W._insert_readings(hc, {"schema": 1, "device_id": "dehum_pm", "device_type": "energy_meter", "area": "attic",
                            "transport": "wifi-mqtt", "ts": "2026-10-04T22:09:00Z", "metrics": {"power_w": 3}})
    tc = _reg()["dehum_attic"].traits_cfg
    from datetime import datetime, timezone
    now = datetime(2026, 10, 4, 22, 10, tzinfo=timezone.utc).timestamp()
    a = verify_power_alert(hc, "dehum_attic", tc, now)
    assert a and a["title"] == "Called but not running" and "3 W" in a["text"]
    W._insert_readings(hc, {"schema": 1, "device_id": "dehum_pm", "device_type": "energy_meter", "area": "attic",
                            "transport": "wifi-mqtt", "ts": "2026-10-04T22:09:30Z", "metrics": {"power_w": 590}})
    assert verify_power_alert(hc, "dehum_attic", tc, now) is None


# ── outdoor dew-point gate (2026-10-04) ──────────────────────────────────────────────────────────────

def _gate(tmp_path, out_t, out_rh, *, age_s=60, dh_running=False):
    ctrl, iss, db = _make(tmp_path, rh=(60, 62), dh_running=dh_running, lease_left=500, erv_mode=11)
    conn = sqlite3.connect(db)
    pol = store.get_policy(conn, "dehum_attic")
    store.set_policy(conn, "dehum_attic", {**pol, "outdoor_gate": {"sensor": "outdoor_s", "min_dewpoint_c": 4.4}})
    conn.close()
    ctrl.readings["outdoor_s"] = {"m": {"temperature_c": out_t, "humidity_pct": out_rh}, "ts": NOW - age_s}
    return ctrl, iss, db


def test_dry_outdoor_air_skips_starting(tmp_path):
    ctrl, iss, _ = _gate(tmp_path, 5.0, 50)                  # dew point ~ -4.6 °C
    ctrl.tick(now=NOW)
    assert ("dehum_attic", "switchable", {"on": True}) not in iss.calls


def test_humid_outdoor_air_lets_it_start(tmp_path):
    ctrl, iss, _ = _gate(tmp_path, 26.7, 38)                 # dew point ~ 11.2 °C
    ctrl.tick(now=NOW)
    assert ("dehum_attic", "switchable", {"on": True}) in iss.calls


def test_gate_does_not_stop_a_running_call(tmp_path):
    ctrl, iss, _ = _gate(tmp_path, 5.0, 50, dh_running=True)
    ctrl.tick(now=NOW)
    assert ("dehum_attic", "switchable", {"on": False}) not in iss.calls


def test_stale_outdoor_reading_fails_open(tmp_path):
    ctrl, iss, _ = _gate(tmp_path, 5.0, 50, age_s=3 * 3600)
    ctrl.tick(now=NOW)
    assert ("dehum_attic", "switchable", {"on": True}) in iss.calls


def test_boost_override_bypasses_the_gate(tmp_path):
    ctrl, iss, db = _gate(tmp_path, 5.0, 50)
    conn = sqlite3.connect(db)
    store.set_override(conn, "dehum_attic", "boost_on", NOW + 3600)
    conn.close()
    ctrl.tick(now=NOW)
    assert ("dehum_attic", "switchable", {"on": True}) in iss.calls


def test_gate_api_validation():
    from server.api.control import handle_policy_update
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    store.set_policy(conn, "dehum_attic", C.DEHUM_POLICY)
    assert handle_policy_update(conn, "dehum_attic", {"outdoor_gate": {"min_dewpoint_c": 99}})[0] == 400
    code, _ = handle_policy_update(conn, "dehum_attic", {"outdoor_gate": {"sensor": "switchbot_outdoor",
                                                                          "min_dewpoint_c": 4.4}})
    assert code == 200 and store.get_policy(conn, "dehum_attic")["outdoor_gate"]["sensor"] == "switchbot_outdoor"
