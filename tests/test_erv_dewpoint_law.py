"""ERV dew-point law (Hugh, 2026-11-11): one level per `step_c` of indoor/outdoor dew-point gap, UP when outdoor air is
drier and DOWN when it's wetter, clamped to off..top, with cold outdoor air fighting the boost and poor indoor air
fighting the trim. Opt-in; supersedes the dry boost and wet trim while enabled. Plus the `function` category the
"track the dehumidifiers" floor finds dehumidifiers by."""
import json
import math
import sqlite3

import pytest

from server.control import control_store as store
from server.control import controller as C
from server.control import dewpoint_law as L
from server.control import functions
from server.control.issuer import Result
from server.control.registry import parse_control_registry

yaml = pytest.importorskip("yaml")
NOW = 2_000_000.0
ROOMS = ["room_a", "room_b"]
T_IN, RH_IN = 21.0, 50.0                 # indoor dew point ~10.2 °C
OFF, LOW, MED, HIGH = 1, 9, 11, 10       # Broan wire codes (config-examples/control.example.yaml)
LAW = {"enabled": True, "step_c": 2.0}
BANDS = [{"max": 60, "level": 3}, {"max": 75, "level": 2}, {"max": None, "level": 1}]   # live on ha-2


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


def _make(tmp_path, *, aq=70.0, erv_mode=MED, law=LAW, extra=None):
    db = str(tmp_path / "control.db")
    conn = sqlite3.connect(db)
    store.ensure_schema(conn)
    pol = {**C.ERV_POLICY, "enabled": True, "source_sensors": ["gas_a"],
           "control": {**C.ERV_POLICY["control"], "bands": BANDS}}
    pol["dry_boost"] = {"outdoor_sensor": "outdoor", "indoor_sensors": ROOMS}
    if law is not None:
        pol["dewpoint_law"] = law
    store.set_policy(conn, "erv_attic", pol)
    for did, p in (extra or {}).items():
        store.set_policy(conn, did, p)
    conn.close()
    iss = Iss()
    ctrl = C.Controller(iss, {}, parse_control_registry(yaml.safe_load(open("config-examples/control.example.yaml"))),
                        db)
    ctrl._aq = aq
    ctrl._latest_stored = lambda sid, metric, n: {"m": {metric: ctrl._aq}, "ts": n - 60}
    ctrl.on_message(None, None, _Msg("erv_attic", {"fan_mode": erv_mode}))
    return ctrl, iss


def _air(ctrl, gap, *, t_out=15.0, rh_in=RH_IN, now=NOW):
    """Indoor T_IN/rh_in; outdoor at t_out with its dew point `gap` °C ABOVE the indoor one (negative = drier)."""
    for r in ROOMS:
        ctrl.readings[r] = {"m": {"temperature_c": T_IN, "humidity_pct": rh_in}, "ts": now - 30}
    rh_out = 100 * _es(_dp(T_IN, rh_in) + gap) / _es(t_out)
    ctrl.readings["outdoor"] = {"m": {"temperature_c": t_out, "humidity_pct": rh_out}, "ts": now - 30}


def _erv(iss):
    return [a for d, t, a in iss.calls if d == "erv_attic"]


# ── the law in the controller (AQ 70 -> band 2 = med) ────────────────────────────────────────────────────────────
def test_wetter_outdoor_air_steps_down_to_off(tmp_path):
    ctrl, iss = _make(tmp_path, aq=80.0)                     # AQ 80: above aq_start 75, no air-quality force
    _air(ctrl, 4.5)
    ctrl.tick(now=NOW)
    assert _erv(iss)[-1] == {"mode": "off"}                  # band level 1 (AQ > 75) − 2 = off (clamped)


def test_drier_outdoor_air_steps_up(tmp_path):
    ctrl, iss = _make(tmp_path)
    _air(ctrl, -2.5)                                         # one step drier, 15 °C out: no cold force
    ctrl.tick(now=NOW)
    assert _erv(iss)[-1] == {"mode": "high"}                 # med + 1


def test_inside_one_step_nothing_changes(tmp_path):
    ctrl, iss = _make(tmp_path)
    _air(ctrl, 1.5)
    ctrl.tick(now=NOW)
    assert _erv(iss) == []


def test_cold_outdoor_air_cancels_the_boost(tmp_path):
    ctrl, iss = _make(tmp_path)
    _air(ctrl, -2.5, t_out=1.0)                              # 10 − 1 = 9 °C below the start: one cold notch
    ctrl.tick(now=NOW)
    assert _erv(iss) == []                                   # dry 1 − cold 1 = 0: stays at med


def test_cold_never_pulls_below_the_air_quality_level(tmp_path):
    ctrl, iss = _make(tmp_path)
    _air(ctrl, 0.0, t_out=-20.0)                             # very cold, no dew-point gap
    ctrl.tick(now=NOW)
    assert _erv(iss) == []


def test_poor_air_cancels_the_trim(tmp_path):
    ctrl, iss = _make(tmp_path, aq=55.0, erv_mode=HIGH)      # band 3; 75 − 55 = 20 -> two air-quality notches
    _air(ctrl, 4.5)                                          # wet 2
    ctrl.tick(now=NOW)
    assert _erv(iss) == []                                   # trim 2 − 2 = 0: stays high


def test_air_and_dew_point_fight(tmp_path):
    ctrl, iss = _make(tmp_path, aq=65.0)                     # band 2; one air-quality notch
    _air(ctrl, 4.5)                                          # wet 2
    ctrl.tick(now=NOW)
    assert _erv(iss)[-1] == {"mode": "low"}                  # med − (2 − 1)


def test_cold_room_blocks_the_boost(tmp_path):
    ctrl, iss = _make(tmp_path)
    _air(ctrl, -4.5)
    for r in ROOMS:
        ctrl.readings[r]["m"]["temperature_c"] = 17.0
    ctrl.tick(now=NOW)
    assert {"mode": "high"} not in _erv(iss)


def test_operator_override_beats_the_law(tmp_path):
    ctrl, iss = _make(tmp_path, aq=80.0)
    conn = sqlite3.connect(str(tmp_path / "control.db"))
    store.set_override(conn, "erv_attic", "boost_on", NOW + 3600)
    conn.commit()
    conn.close()
    _air(ctrl, 4.5)
    ctrl.tick(now=NOW)
    assert {"mode": "off"} not in _erv(iss) and {"mode": "low"} not in _erv(iss)


def test_disabled_law_leaves_the_dry_boost_running(tmp_path):
    ctrl, iss = _make(tmp_path, law={"enabled": False, "step_c": 2.0})
    _air(ctrl, -2.5)                                         # Δ 2.5 ≥ the dry boost's 2.0 on-delta
    ctrl.tick(now=NOW)
    assert _erv(iss)[-1] == {"mode": "high"}                 # the old boost, unchanged
    ctrl2, iss2 = _make(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path,
                        law={"enabled": False, "step_c": 2.0}, aq=80.0)
    _air(ctrl2, 4.5)
    ctrl2.tick(now=NOW)
    assert {"mode": "off"} not in _erv(iss2)                 # and no trim


def test_changed_level_is_not_compounded(tmp_path):
    ctrl, iss = _make(tmp_path, aq=72.0)                     # band 2, within the deadband of the 75 edge
    _air(ctrl, 2.5)
    ctrl.tick(now=NOW)
    assert _erv(iss)[-1] == {"mode": "low"}
    ctrl.on_message(None, None, _Msg("erv_attic", {"fan_mode": LOW}))
    n = len(_erv(iss))
    for k in range(1, 4):
        _air(ctrl, 2.5, now=NOW + 60 * k)
        ctrl.tick(now=NOW + 60 * k)
    assert len(_erv(iss)) == n                               # stays at low, never stepped to off


# ── the "track the dehumidifiers" floor ───────────────────────────────────────────────────────────────────────────
def _dehum_pol(off_below, *, enabled=True):
    return {"enabled": enabled, "source_sensor": "meter_lr",
            "control": {"strategy": "hysteresis", "metric": "humidity_pct", "on_above": off_below + 4,
                        "off_below": off_below, "min_on_min": 10, "min_off_min": 10}}


def _lr(ctrl, rh, now=NOW):
    ctrl.readings["meter_lr"] = {"m": {"humidity_pct": rh, "temperature_c": 21.0}, "ts": now - 30}


def test_dehumidifier_floor_trims_while_a_dehumidifier_would_run(tmp_path):
    ctrl, iss = _make(tmp_path, aq=80.0, law={**LAW, "trim_rh_floor": "dehumidifiers"},
                      extra={"dehum_attic": _dehum_pol(40)})
    _lr(ctrl, 43.0)                                          # above the Aprilaire's 40 shut-off
    _air(ctrl, 2.5)
    ctrl.tick(now=NOW)
    assert {"mode": "off"} in _erv(iss)                     # band 1 − 1


def test_dehumidifier_floor_stops_the_trim_below_every_shutoff(tmp_path):
    ctrl, iss = _make(tmp_path, aq=80.0, erv_mode=LOW, law={**LAW, "trim_rh_floor": "dehumidifiers"},
                      extra={"dehum_attic": _dehum_pol(45)})
    _lr(ctrl, 43.0)
    _air(ctrl, 2.5)
    ctrl.tick(now=NOW)
    assert {"mode": "off"} not in _erv(iss)


def test_a_parked_dehumidifier_does_not_count(tmp_path):
    ctrl, iss = _make(tmp_path, aq=80.0, erv_mode=LOW, law={**LAW, "trim_rh_floor": "dehumidifiers"},
                      extra={"dehum_attic": _dehum_pol(40)})
    conn = sqlite3.connect(str(tmp_path / "control.db"))
    store.set_override(conn, "dehum_attic", "off", NOW + 3600)
    conn.commit()
    conn.close()
    _lr(ctrl, 43.0)
    _air(ctrl, 2.5)
    ctrl.tick(now=NOW)
    assert {"mode": "off"} not in _erv(iss)                 # no running dehumidifier automation: no trim


# ── the pure law ──────────────────────────────────────────────────────────────────────────────────────────────────
CFG = L.config({"enabled": True, "step_c": 2.0}, {"outdoor_sensor": "o", "indoor_sensors": ["i"]})


def _x(gap, *, t_out=15.0, aq=80.0, rh=50.0, room=21.0, gap_trim=None, dehum=None):
    return L.Inputs(gap_boost=gap, gap_trim=gap if gap_trim is None else gap_trim, t_out=t_out, aq=aq, rh_in=rh,
                    t_room_min=room, dehum_ok=dehum)


def test_terms_signs_and_forces():
    assert L.terms(CFG, _x(4.5)).net == 2                    # drier outdoors by 2 steps
    assert L.terms(CFG, _x(-4.5)).net == -2
    assert L.terms(CFG, _x(4.5, t_out=1.0)).net == 1         # one cold notch
    assert L.terms(CFG, _x(4.5, t_out=-8.0)).net == 0        # two cold notches cancel both
    assert L.terms(CFG, _x(-4.5, aq=60.0)).net == -1         # one air-quality notch
    assert L.terms(CFG, _x(-4.5, aq=None)).net == 0          # air quality unknown: no trim


def test_doubt_shrinks_both_directions():
    # the sensor says outdoor air is drier, Open-Meteo says wetter: boost uses the wetter, trim the drier
    assert L.terms(CFG, _x(-0.5, gap_trim=2.5)).net == 0


def test_rh_floors():
    cfg = {**CFG, "boost_rh_floor": 40, "trim_rh_floor": 45}
    assert L.terms(cfg, _x(4.5, rh=39.0)).net == 0
    assert L.terms(cfg, _x(-4.5, rh=44.0)).net == 0
    assert L.terms(cfg, _x(-4.5, rh=46.0)).net == -2
    dh = {**CFG, "trim_rh_floor": "dehumidifiers"}
    assert L.terms(dh, _x(-4.5, dehum=True)).net == -2
    assert L.terms(dh, _x(-4.5, dehum=False)).net == 0 and L.terms(dh, _x(-4.5, dehum=None)).net == 0


def test_hysteresis_and_min_hold():
    assert L.decide(CFG, _x(-3.7), -2, NOW - 3600, NOW)[0] == -2     # within 0.5 of the 4.0 edge: hold
    assert L.decide(CFG, _x(-3.4), -2, NOW - 3600, NOW)[0] == -1
    assert L.decide(CFG, _x(-4.5), -1, NOW - 60, NOW)[0] == -1       # wants −2, held 1 min of 15
    assert L.decide(CFG, None, -2, NOW - 60, NOW)[0] == 0            # inputs gone: release at once
    assert L.decide(CFG, _x(4.5, room=17.0), 2, NOW - 60, NOW)[0] == 0   # cold room: at once


def test_outdoor_temperature_crosscheck_takes_the_colder():
    cfg = {**CFG, "reference": "openmeteo"}
    assert L.outdoor_temperature(14.0, (13.0, 80.0, NOW), cfg, NOW)[0] == 14.0
    t, src = L.outdoor_temperature(18.0, (12.0, 80.0, NOW), cfg, NOW)
    assert t == 12.0 and "colder" in src
    assert L.outdoor_temperature(18.0, (12.0, 80.0, NOW - 7200), cfg, NOW)[0] == 18.0   # stale reference


def test_config_and_enabled():
    assert not L.enabled(L.config({"step_c": 2.0}, {"outdoor_sensor": "o", "indoor_sensors": ["i"]}))  # off by default
    assert L.enabled(CFG)
    assert L.config({"trim_rh_floor": None})["trim_rh_floor"] is None


def test_api_validation():
    from server.api.control import handle_policy_update
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    store.set_policy(conn, "erv_attic", C.ERV_POLICY)
    assert handle_policy_update(conn, "erv_attic", {"dewpoint_law": {"step_c": 2}})[0] == 400   # no sensors
    store.set_policy(conn, "erv_attic", {**C.ERV_POLICY, "dry_boost": {"outdoor_sensor": "o", "indoor_sensors": ROOMS}})
    assert handle_policy_update(conn, "erv_attic", {"dewpoint_law": {"enabled": True}})[0] == 400   # no step
    assert handle_policy_update(conn, "erv_attic", {"dewpoint_law": {"trim_rh_floor": "dehumidifier"}})[0] == 400
    assert handle_policy_update(conn, "erv_attic", {"dewpoint_law": {"cold_step_c": 0.2}})[0] == 400
    code, body = handle_policy_update(conn, "erv_attic", {"dewpoint_law": {"enabled": True, "step_c": 1.67,
                                                                           "trim_rh_floor": "dehumidifiers"}})
    assert code == 200, body


# ── function (category) ───────────────────────────────────────────────────────────────────────────────────────────
def test_function_vocabulary_is_closed_and_needs_a_capable_trait():
    assert functions.validate("x", None, []) is None
    assert functions.validate("x", "dehumidify", ["switchable"]) is None
    assert "unknown function" in functions.validate("x", "dehumidifier", ["switchable"])
    assert "needs one of" in functions.validate("x", "dehumidify", ["indicator"])
    with pytest.raises(ValueError):
        parse_control_registry({"devices": {"d": {"node": "n", "area": "a", "traits": {"indicator": {}},
                                                  "function": "dehumidify"}}})
