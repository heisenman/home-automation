"""ERV dew-point wet trim (Hugh, 2026-11-10): one level lower per `step_c` of outdoor-over-indoor dew point, all the
way down to off. Opt-in (step_c null = disabled). Released at once by bad air quality, a dry house, or stale
readings; hysteresis + min-hold otherwise. Safety / operator overrides still win."""
import json
import math
import sqlite3

import pytest

from server.control import control_store as store
from server.control import controller as C
from server.control import wet_trim as W
from server.control.issuer import Result
from server.control.registry import parse_control_registry

yaml = pytest.importorskip("yaml")
NOW = 2_000_000.0
ROOMS = ["room_a", "room_b"]
T_IN, RH_IN = 21.0, 50.0                 # indoor dew point ~10.2 °C
OFF, LOW, MED, HIGH = 1, 9, 11, 10       # Broan wire codes (config-examples/control.example.yaml)


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


def _es(t):
    return 6.112 * math.exp(17.62 * t / (243.12 + t))


def _dp(t, rh):
    g = math.log(rh / 100) + 17.62 * t / (243.12 + t)
    return 243.12 * g / (17.62 - g)


def _make(tmp_path, *, aq=45.0, erv_mode=MED, trim=None):
    db = str(tmp_path / "control.db")
    conn = sqlite3.connect(db)
    store.ensure_schema(conn)
    pol = {**C.ERV_POLICY, "enabled": True, "source_sensors": ["gas_a"]}
    pol["dry_boost"] = {"outdoor_sensor": "outdoor", "indoor_sensors": ROOMS}
    if trim is not None:
        pol["wet_trim"] = trim
    store.set_policy(conn, "erv_attic", pol)
    conn.close()
    iss = Iss()
    ctrl = C.Controller(iss, {}, parse_control_registry(yaml.safe_load(open("config-examples/control.example.yaml"))),
                        db)
    ctrl._aq = aq
    ctrl._latest_stored = lambda sid, metric, n: {"m": {metric: ctrl._aq}, "ts": n - 60}
    ctrl.on_message(None, None, _Msg("erv_attic", {"fan_mode": erv_mode}))
    return ctrl, iss


def _air(ctrl, gap, *, rh_in=RH_IN, now=NOW):
    """Indoor T_IN/rh_in; outdoor at 15 °C with its dew point `gap` °C above the indoor one."""
    for r in ROOMS:
        ctrl.readings[r] = {"m": {"temperature_c": T_IN, "humidity_pct": rh_in}, "ts": now - 30}
    rh_out = 100 * _es(_dp(T_IN, rh_in) + gap) / _es(15.0)
    ctrl.readings["outdoor"] = {"m": {"temperature_c": 15.0, "humidity_pct": rh_out}, "ts": now - 30}


def _erv(iss):
    return [a for d, t, a in iss.calls if d == "erv_attic"]


TRIM = {"step_c": 2.0, "release_below": 30}     # AQ 45 -> med (band 2) and above the release


def test_disabled_by_default(tmp_path):
    ctrl, iss = _make(tmp_path)
    _air(ctrl, 5.0)
    ctrl.tick(now=NOW)
    assert _erv(iss) == []                                  # AQ says med, already med


def test_one_step_drops_one_level(tmp_path):
    ctrl, iss = _make(tmp_path, trim=TRIM)
    _air(ctrl, 2.5)
    ctrl.tick(now=NOW)
    assert _erv(iss)[-1] == {"mode": "low"}


def test_two_steps_from_med_reach_off(tmp_path):
    ctrl, iss = _make(tmp_path, trim=TRIM)
    _air(ctrl, 4.5)
    ctrl.tick(now=NOW)
    assert _erv(iss)[-1] == {"mode": "off"}


def test_drier_outdoor_air_is_untouched(tmp_path):
    ctrl, iss = _make(tmp_path, trim=TRIM)
    _air(ctrl, -1.5)
    ctrl.tick(now=NOW)
    assert _erv(iss) == []


def test_bad_air_releases_the_trim(tmp_path):
    ctrl, iss = _make(tmp_path, aq=25.0, erv_mode=HIGH, trim=TRIM)    # band 3, at/below release_below
    _air(ctrl, 6.0)
    ctrl.tick(now=NOW)
    assert _erv(iss) == []                                  # stays high


def test_dry_house_releases_the_trim(tmp_path):
    ctrl, iss = _make(tmp_path, trim=TRIM)
    _air(ctrl, 4.5, rh_in=42.0)
    ctrl.tick(now=NOW)
    assert _erv(iss) == []


def test_operator_boost_beats_the_trim(tmp_path):
    ctrl, iss = _make(tmp_path, trim=TRIM)
    conn = sqlite3.connect(ctrl.db_path) if hasattr(ctrl, "db_path") else sqlite3.connect(str(tmp_path / "control.db"))
    store.set_override(conn, "erv_attic", "boost_on", NOW + 3600)
    conn.commit()
    conn.close()
    _air(ctrl, 4.5)
    ctrl.tick(now=NOW)
    assert {"mode": "off"} not in _erv(iss) and {"mode": "low"} not in _erv(iss)


def test_trimmed_level_is_not_held_and_trimmed_again(tmp_path):
    # AQ 58 -> med, within ±5 of the 60 edge: the AQ rule alone would hold a device sitting at low
    ctrl, iss = _make(tmp_path, aq=58.0, trim=TRIM)
    _air(ctrl, 2.5)
    ctrl.tick(now=NOW)
    assert _erv(iss)[-1] == {"mode": "low"}
    ctrl.on_message(None, None, _Msg("erv_attic", {"fan_mode": LOW}))
    n = len(_erv(iss))
    for k in range(1, 4):
        _air(ctrl, 2.5, now=NOW + 60 * k)
        ctrl.tick(now=NOW + 60 * k)
    assert len(_erv(iss)) == n                              # stays at low, never stepped to off


def test_hysteresis_and_min_hold_then_back_to_the_band_level(tmp_path):
    ctrl, iss = _make(tmp_path, aq=58.0, trim=TRIM)
    _air(ctrl, 2.5)
    ctrl.tick(now=NOW)
    ctrl.on_message(None, None, _Msg("erv_attic", {"fan_mode": LOW}))
    _air(ctrl, 1.7, now=NOW + 1200)                         # inside the 0.5 °C hysteresis: hold the drop
    ctrl.tick(now=NOW + 1200)
    assert _erv(iss)[-1] == {"mode": "low"} and len(_erv(iss)) == 1
    _air(ctrl, 0.5, now=NOW + 1260)
    ctrl.tick(now=NOW + 1260)
    assert _erv(iss)[-1] == {"mode": "med"}                 # the band level, not a band-edge hold at low


def test_drops_for_hysteresis():
    assert W.drops_for(1.9, 2.0, 0, 0.5) == 0
    assert W.drops_for(4.1, 2.0, 0, 0.5) == 2
    assert W.drops_for(3.6, 2.0, 2, 0.5) == 2               # within hysteresis of the 4.0 edge
    assert W.drops_for(3.4, 2.0, 2, 0.5) == 1
    assert W.drops_for(-1.0, 2.0, 2, 0.5) == 0


def test_min_hold_delays_a_change_but_not_a_release():
    cfg = W.config({"step_c": 2.0}, {"outdoor_sensor": "o", "indoor_sensors": ["i"]})
    from server.control.dry_boost import Conditions
    wet = Conditions(dp_in=10.0, dp_out=14.5, rh_in=50.0, t_min=21.0, n_in=1)
    assert W.decide(cfg, wet, 80.0, 1, NOW - 60, NOW)[0] == 1          # wants 2, held 1 min of 15
    assert W.decide(cfg, wet, 80.0, 1, NOW - 960, NOW)[0] == 2
    assert W.decide(cfg, wet, 50.0, 2, NOW - 60, NOW)[0] == 0          # AQ 50 ≤ 60: released at once


def test_config_falls_back_to_the_dry_boost_sensors():
    cfg = W.config({"step_c": 3}, {"outdoor_sensor": "o", "indoor_sensors": ["i"], "reference": "openmeteo"})
    assert cfg["outdoor_sensor"] == "o" and cfg["indoor_sensors"] == ["i"] and cfg["reference"] == "openmeteo"
    assert W.enabled(cfg) and not W.enabled(W.config({"step_c": None}, {"outdoor_sensor": "o"}))
    assert W.config({"release_below": None})["release_below"] is None


def test_crosscheck_doubt_takes_the_drier():
    from server.control import dry_boost as D
    cfg = W.config({"step_c": 2}, {"outdoor_sensor": "o", "indoor_sensors": ["i"], "reference": "openmeteo"})
    dp, src = D.outdoor_dewpoint((15.0, 75.0), (15.0, 55.0, 1000.0), cfg, 1000.0, doubt="drier")
    assert dp == D.dewpoint_c(15.0, 55.0) and "drier" in src


def test_api_validation():
    from server.api.control import handle_policy_update
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    store.set_policy(conn, "erv_attic", C.ERV_POLICY)
    assert handle_policy_update(conn, "erv_attic", {"wet_trim": {"step_c": 2}})[0] == 400     # no sensors anywhere
    store.set_policy(conn, "erv_attic", {**C.ERV_POLICY, "dry_boost": {"outdoor_sensor": "o", "indoor_sensors": ROOMS}})
    assert handle_policy_update(conn, "erv_attic", {"wet_trim": {"step_c": 0.1}})[0] == 400
    assert handle_policy_update(conn, "erv_attic", {"wet_trim": {"step_c": 1, "hysteresis_c": 1}})[0] == 400
    code, _ = handle_policy_update(conn, "erv_attic", {"wet_trim": {"step_c": 2.5, "release_below": None}})
    assert code == 200 and store.get_policy(conn, "erv_attic")["wet_trim"] == {"step_c": 2.5, "release_below": None}
    code, _ = handle_policy_update(conn, "erv_attic", {"wet_trim": {"step_c": None}})
    assert code == 200
