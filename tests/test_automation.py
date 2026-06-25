"""Tests for the pure automation-policy resolver (server/control/automation.py, ADR-0011)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.control.automation import (  # noqa: E402
    Policy, Reading, DeviceState, Override, resolve, in_window, schedule_off_now,
    apply_scene, HOUSE_SCENES, DEFAULT_SCENE)
from tests._harness import run_module  # noqa: E402

NOW = 1_000_000.0
# generous cycle limits so transitions are allowed unless a test sets last_on/off recently
P = Policy(on_above=55, off_below=50, min_on_s=600, min_off_s=300, default_running=False)


def _state(running, **kw):
    # default last_on/off far in the past so cycle gating doesn't block unless overridden
    kw.setdefault("last_on_ts", NOW - 10_000)
    kw.setdefault("last_off_ts", NOW - 10_000)
    return DeviceState(running=running, **kw)


def fresh(value):
    return Reading(value=value, ts=NOW - 30)


# ── hysteresis rule ──────────────────────────────────────────────────────────────
def test_hysteresis_turns_on_above():
    r = resolve(P, NOW, fresh(57), _state(False))
    assert r.running and r.act and r.source == "rule"


def test_hysteresis_turns_off_below():
    r = resolve(P, NOW, fresh(48), _state(True))
    assert not r.running and r.act and r.source == "rule"


def test_hysteresis_deadband_holds_state():
    on = resolve(P, NOW, fresh(52), _state(True))
    off = resolve(P, NOW, fresh(52), _state(False))
    assert on.running and not on.act and "deadband" in on.reason
    assert not off.running and not off.act


def test_no_op_when_already_in_desired_state():
    r = resolve(P, NOW, fresh(57), _state(True))   # wants ON, already ON
    assert r.running and not r.act and "no-op" in r.reason


# ── compressor cycle gating ──────────────────────────────────────────────────────
def test_min_off_blocks_restart():
    st = _state(False, last_off_ts=NOW - 100)       # off only 100s ago, min_off=300
    r = resolve(P, NOW, fresh(60), st)              # rule wants ON
    assert not r.running and not r.act and "min-off" in r.reason


def test_min_on_blocks_rapid_off_for_rule():
    st = _state(True, last_on_ts=NOW - 60)          # on only 60s ago, min_on=600
    r = resolve(P, NOW, fresh(45), st)              # rule wants OFF
    assert r.running and not r.act and "min-on" in r.reason


# ── safety interlocks ────────────────────────────────────────────────────────────
def test_interlock_forces_off_immediately_bypassing_min_on():
    st = _state(True, interlocks=("tank_full",), last_on_ts=NOW - 5)
    r = resolve(P, NOW, fresh(60), st)              # sensor would want ON
    assert not r.running and r.act and r.source == "safety" and "tank_full" in r.reason


def test_interlock_beats_override_boost():
    st = _state(True, interlocks=("error",))
    r = resolve(P, NOW, fresh(60), st, override=Override("boost_on"))
    assert not r.running and r.source == "safety"   # safety outranks override


# ── manual override (TTL) ────────────────────────────────────────────────────────
def test_override_off_beats_rule():
    r = resolve(P, NOW, fresh(60), _state(True), override=Override("off", expiry=NOW + 3600))
    assert not r.running and r.act and r.source == "override"


def test_override_off_immediate_bypasses_min_on():
    st = _state(True, last_on_ts=NOW - 5)           # just turned on
    r = resolve(P, NOW, fresh(60), st, override=Override("off", expiry=NOW + 600))
    assert not r.running and r.act and r.source == "override"


def test_override_boost_on_respects_min_off():
    st = _state(False, last_off_ts=NOW - 50)        # off 50s ago, min_off=300
    r = resolve(P, NOW, fresh(45), st, override=Override("boost_on", expiry=NOW + 600))
    assert not r.running and not r.act and "min-off" in r.reason


def test_expired_override_falls_through_to_rule():
    r = resolve(P, NOW, fresh(60), _state(False), override=Override("off", expiry=NOW - 1))
    assert r.running and r.source == "rule"          # expired -> rule wins


# ── schedule ─────────────────────────────────────────────────────────────────────
def test_schedule_off_beats_rule():
    r = resolve(P, NOW, fresh(60), _state(True), schedule_off=True)
    assert not r.running and r.source == "schedule"


# ── sensor freshness / default ───────────────────────────────────────────────────
def test_stale_sensor_falls_to_default():
    stale = Reading(value=60, ts=NOW - 5000)        # older than sensor_stale_s
    r = resolve(P, NOW, stale, _state(True))
    assert r.source == "default" and not r.running   # default_running False -> off


def test_missing_sensor_falls_to_default():
    r = resolve(P, NOW, None, _state(True))
    assert r.source == "default"


# ── setpoint strategy ────────────────────────────────────────────────────────────
def test_setpoint_strategy_keeps_device_on():
    sp = Policy(strategy="setpoint", min_off_s=300)
    r = resolve(sp, NOW, None, _state(False))
    assert r.running and r.source == "rule" and "self-regulate" in r.reason


# ── schedule window helpers ──────────────────────────────────────────────────────
def test_in_window_simple():
    assert in_window(9 * 60, "08:00-17:00")
    assert not in_window(18 * 60, "08:00-17:00")


def test_in_window_wraps_midnight():
    assert in_window(23 * 60, "22:00-07:00")        # 11pm
    assert in_window(3 * 60, "22:00-07:00")         # 3am
    assert not in_window(12 * 60, "22:00-07:00")    # noon


def test_schedule_off_now_matches_off_window_only():
    sched = [{"when": "22:00-07:00", "policy": "off"}, {"when": "07:00-22:00", "policy": "auto"}]
    assert schedule_off_now(sched, 23 * 60)          # in the off window
    assert not schedule_off_now(sched, 12 * 60)      # in the auto window


# ── Policy.from_dict ─────────────────────────────────────────────────────────────
def test_policy_from_dict_minutes_to_seconds():
    p = Policy.from_dict({"control": {"strategy": "hysteresis", "on_above": 60, "off_below": 52,
                                      "min_on_min": 10, "min_off_min": 5}})
    assert p.on_above == 60 and p.off_below == 52 and p.min_on_s == 600 and p.min_off_s == 300


# ── house scenes (Home/Away/Sleep) ────────────────────────────────────────────────
_SCENE_POL = {"control": {"on_above": 44, "off_below": 40},
              "scenes": {"Away": {"on_above": 60, "off_below": 55}, "Sleep": {"off": True}}}


def test_default_scene_is_home_and_in_canon():
    assert DEFAULT_SCENE == "Home" and "Home" in HOUSE_SCENES and "Away" in HOUSE_SCENES


def test_apply_scene_no_profile_returns_policy_unchanged():
    eff, off = apply_scene(_SCENE_POL, "Home")          # Home has no profile
    assert eff is _SCENE_POL and off is False


def test_apply_scene_none_name_is_noop():
    eff, off = apply_scene(_SCENE_POL, None)
    assert eff is _SCENE_POL and off is False


def test_apply_scene_away_relaxes_thresholds_without_mutating_base():
    eff, off = apply_scene(_SCENE_POL, "Away")
    assert off is False
    assert eff["control"]["on_above"] == 60 and eff["control"]["off_below"] == 55
    assert _SCENE_POL["control"]["on_above"] == 44       # base dict untouched


def test_apply_scene_sleep_forces_off():
    eff, off = apply_scene(_SCENE_POL, "Sleep")
    assert off is True


def test_scene_off_layer_parks_device_with_scene_source():
    # past min-on so an OFF transition is allowed
    st = _state(True)
    r = resolve(P, NOW, fresh(70), st, scene_off=True, scene="Sleep")
    assert r.running is False and r.act and r.source == "scene" and "Sleep" in r.reason


def test_manual_override_beats_scene_off():
    ov = Override("boost_on", None)
    r = resolve(P, NOW, fresh(30), _state(False), override=ov, scene_off=True, scene="Sleep")
    assert r.running is True and r.source == "override"


def test_scene_off_beats_schedule_off():
    # both set; scene is the higher layer, so the reason attributes to the scene
    r = resolve(P, NOW, fresh(70), _state(True), scene_off=True, schedule_off=True, scene="Away")
    assert r.source == "scene"


def test_away_relaxation_changes_decision_vs_home():
    # RH 57: ON under Home (>=44), but only deadband-hold under Away (44<57<60)
    home_pol = Policy.from_dict(apply_scene(_SCENE_POL, "Home")[0])
    away_pol = Policy.from_dict(apply_scene(_SCENE_POL, "Away")[0])
    home = resolve(home_pol, NOW, fresh(57), _state(False))
    away = resolve(away_pol, NOW, fresh(57), _state(False))
    assert home.running is True and home.source == "rule"
    assert away.running is False and "deadband" in away.reason


if __name__ == "__main__":
    run_module(globals())
