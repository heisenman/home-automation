"""ERV dew-point dry boost (Hugh, 2026-11-04): boost to high while outdoor air is >= 2 °C (dew point) drier than
the house; release under 1 °C, or at once at the 40 % RH floor or a cold room. Boost wins over the air-quality
level; otherwise the air-quality rule runs unchanged. Safety / operator overrides still win."""
import json
import sqlite3

import pytest

from server.control import control_store as store
from server.control import controller as C
from server.control import dry_boost as D
from server.control.issuer import Result
from server.control.registry import parse_control_registry

yaml = pytest.importorskip("yaml")
NOW = 2_000_000.0
ROOMS = ["room_a", "room_b"]


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


def _make(tmp_path, *, aq=90.0, erv_mode=9, boost=None):
    db = str(tmp_path / "control.db")
    conn = sqlite3.connect(db)
    store.ensure_schema(conn)
    pol = {**C.ERV_POLICY, "enabled": True, "source_sensors": ["gas_a"]}
    pol["dry_boost"] = {"outdoor_sensor": "outdoor", "indoor_sensors": ROOMS, **(boost or {})}
    store.set_policy(conn, "erv_attic", pol)
    conn.close()
    iss = Iss()
    ctrl = C.Controller(iss, {}, parse_control_registry(yaml.safe_load(open("config-examples/control.example.yaml"))),
                        db)
    ctrl._latest_stored = lambda sid, metric, n: {"m": {metric: aq}, "ts": n - 60}   # AQ 90: clean -> low
    ctrl.on_message(None, None, _Msg("erv_attic", {"fan_mode": erv_mode}))
    return ctrl, iss, db


def _air(ctrl, *, t_in=21.0, rh_in=45.0, t_out=10.0, rh_out=60.0, age_s=60, now=NOW):
    for r in ROOMS:
        ctrl.readings[r] = {"m": {"temperature_c": t_in, "humidity_pct": rh_in}, "ts": now - 30}
    ctrl.readings["outdoor"] = {"m": {"temperature_c": t_out, "humidity_pct": rh_out}, "ts": now - age_s}


def _erv(iss):
    return [a for d, t, a in iss.calls if d == "erv_attic"]


# indoor 21 °C / 45 % -> dp 8.6 °C; outdoor 10 °C / 60 % -> dp 2.4 °C (Δ 6.2); 10 °C / 90 % -> dp 8.4 (Δ 0.2)

def test_dry_outdoor_air_boosts_to_high_over_the_aq_level(tmp_path):
    ctrl, iss, _ = _make(tmp_path)
    _air(ctrl)
    ctrl.tick(now=NOW)
    assert _erv(iss)[-1] == {"mode": "high"}


def test_wetter_outdoor_air_leaves_the_aq_rule_alone(tmp_path):
    ctrl, iss, _ = _make(tmp_path)
    _air(ctrl, rh_out=90.0)
    ctrl.tick(now=NOW)
    assert _erv(iss) == []                                  # AQ says low, already low


def test_rh_floor_blocks_the_boost(tmp_path):
    ctrl, iss, _ = _make(tmp_path)
    _air(ctrl, rh_in=40.0)
    ctrl.tick(now=NOW)
    assert _erv(iss) == []


def test_cold_room_blocks_the_boost(tmp_path):
    ctrl, iss, _ = _make(tmp_path)
    _air(ctrl, t_in=17.5, rh_in=50.0)
    ctrl.tick(now=NOW)
    assert _erv(iss) == []


def test_stale_outdoor_reading_means_no_boost(tmp_path):
    ctrl, iss, _ = _make(tmp_path)
    _air(ctrl, age_s=30 * 60)
    ctrl.tick(now=NOW)
    assert _erv(iss) == []


def _boosted(tmp_path, **kw):
    ctrl, iss, db = _make(tmp_path, **kw)
    _air(ctrl)
    ctrl.tick(now=NOW)
    assert _erv(iss)[-1] == {"mode": "high"}
    ctrl.on_message(None, None, _Msg("erv_attic", {"fan_mode": 10}))
    return ctrl, iss, db


def test_gap_closing_holds_for_min_on_then_returns_to_the_aq_level(tmp_path):
    ctrl, iss, _ = _boosted(tmp_path)
    _air(ctrl, rh_out=90.0, now=NOW + 600)
    ctrl.tick(now=NOW + 600)                                # 10 min in: min-on holds high
    assert _erv(iss)[-1] == {"mode": "high"}
    _air(ctrl, rh_out=90.0, now=NOW + 1900)
    ctrl.tick(now=NOW + 1900)
    assert _erv(iss)[-1] == {"mode": "low"}                 # AQ band level, not a deadband hold at high


def test_aq_deadband_does_not_hold_the_boosted_level_after_release(tmp_path):
    # AQ 42 -> med, but within ±5 of the 40 edge (<40 -> high), so the AQ rule alone would "hold" high
    ctrl, iss, _ = _boosted(tmp_path, aq=42.0)
    _air(ctrl, rh_out=90.0, now=NOW + 1900)
    ctrl.tick(now=NOW + 1900)
    assert _erv(iss)[-1] == {"mode": "med"}


def test_rh_floor_releases_at_once(tmp_path):
    ctrl, iss, _ = _boosted(tmp_path)
    _air(ctrl, rh_in=39.5, now=NOW + 120)
    ctrl.tick(now=NOW + 120)
    assert _erv(iss)[-1] == {"mode": "low"}


def test_restart_waits_min_off(tmp_path):
    ctrl, iss, _ = _boosted(tmp_path)
    _air(ctrl, rh_in=39.5, now=NOW + 120)
    ctrl.tick(now=NOW + 120)
    ctrl.on_message(None, None, _Msg("erv_attic", {"fan_mode": 9}))
    _air(ctrl, now=NOW + 600)
    ctrl.tick(now=NOW + 600)
    assert _erv(iss)[-1] == {"mode": "low"}                 # still waiting
    _air(ctrl, now=NOW + 120 + 1860)
    ctrl.tick(now=NOW + 120 + 1860)
    assert _erv(iss)[-1] == {"mode": "high"}


def test_operator_off_override_beats_the_boost(tmp_path):
    ctrl, iss, db = _make(tmp_path)
    conn = sqlite3.connect(db)
    store.set_override(conn, "erv_attic", "off", NOW + 3600)
    conn.close()
    _air(ctrl)
    ctrl.tick(now=NOW)
    assert {"mode": "high"} not in _erv(iss)


def test_bad_aq_already_at_high_is_left_alone(tmp_path):
    ctrl, iss, _ = _make(tmp_path, aq=20.0, erv_mode=10)    # AQ 20 -> high already
    _air(ctrl)
    ctrl.tick(now=NOW)
    assert _erv(iss) == []


def test_no_outdoor_sensor_means_disabled(tmp_path):
    ctrl, iss, _ = _make(tmp_path, boost={"outdoor_sensor": None})
    _air(ctrl)
    ctrl.tick(now=NOW)
    assert _erv(iss) == []


def test_decide_hysteresis_band():
    cfg = D.config({"outdoor_sensor": "o", "indoor_sensors": ["r"]})
    c = D.Conditions(dp_in=8.0, dp_out=6.5, rh_in=45, t_min=20, n_in=1)    # Δ 1.5: between off 1 and on 2
    assert D.decide(cfg, c, False, None, NOW)[0] is False
    assert D.decide(cfg, c, True, NOW - 3600, NOW)[0] is True


def test_api_validation():
    from server.api.control import handle_policy_update
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    store.set_policy(conn, "erv_attic", C.ERV_POLICY)
    assert handle_policy_update(conn, "erv_attic", {"dry_boost": {"outdoor_sensor": "o"}})[0] == 400
    assert handle_policy_update(conn, "erv_attic", {"dry_boost": {"on_delta_c": 1, "off_delta_c": 1}})[0] == 400
    assert handle_policy_update(conn, "erv_attic", {"dry_boost": {"level": 9}})[0] == 400
    code, _ = handle_policy_update(conn, "erv_attic", {"dry_boost": {"outdoor_sensor": "switchbot_outdoor",
                                                                    "indoor_sensors": ROOMS}})
    assert code == 200 and store.get_policy(conn, "erv_attic")["dry_boost"]["outdoor_sensor"] == "switchbot_outdoor"


# ── outdoor cross-check against Open-Meteo (Hugh, 2026-11-08: the outdoor meter reads RH high when wet) ──

def _xc(**kw):
    from server.control import dry_boost as db
    return db.config({"outdoor_sensor": "out", "indoor_sensors": ["in"], "reference": "openmeteo", **kw})


def test_crosscheck_agree_uses_sensor():
    from server.control import dry_boost as db
    dp, src = db.outdoor_dewpoint((10.0, 80.0), (10.0, 78.0, 1000.0), _xc(), 1000.0)
    assert src.startswith("sensor") and dp == db.dewpoint_c(10.0, 80.0)


def test_crosscheck_wet_sensor_uses_openmeteo():
    from server.control import dry_boost as db
    # 2026-11-08 13:00: sensor 15.1 °C / 77 % reads dp ~11; Open-Meteo 13.6 / 67 % → ~7.6 (kitchen fell to 7.1)
    dp, src = db.outdoor_dewpoint((15.1, 90.0), (13.6, 67.0, 1000.0), _xc(), 1000.0)
    assert src.startswith("Open-Meteo") and dp == db.dewpoint_c(13.6, 67.0)


def test_crosscheck_dry_sensor_disagreeing_takes_wetter():
    from server.control import dry_boost as db
    # sensor wetter but not in its wet regime (RH < wet_rh): no reason to distrust it → the wetter (sensor)
    dp, src = db.outdoor_dewpoint((15.0, 75.0), (15.0, 55.0, 1000.0), _xc(), 1000.0)
    assert dp == db.dewpoint_c(15.0, 75.0) and "wetter" in src
    # Open-Meteo wetter (the model can miss local dry air the other way): still the wetter
    dp, src = db.outdoor_dewpoint((15.0, 55.0), (15.0, 75.0, 1000.0), _xc(), 1000.0)
    assert dp == db.dewpoint_c(15.0, 75.0)


def test_crosscheck_stale_or_off_falls_back_to_sensor():
    from server.control import dry_boost as db
    stale = (13.6, 67.0, 1000.0 - 46 * 60)
    assert db.outdoor_dewpoint((15.1, 90.0), stale, _xc(), 1000.0)[1] == "sensor"
    assert db.outdoor_dewpoint((15.1, 90.0), None, _xc(), 1000.0)[1] == "sensor"
    assert db.outdoor_dewpoint((15.1, 90.0), (13.6, 67.0, 1000.0), _xc(reference=None), 1000.0)[1] == "sensor"


def test_crosscheck_flows_into_decide():
    from server.control import dry_boost as db
    cfg = _xc()
    now = 10_000.0
    readings = {"out": {"m": {"temperature_c": 15.1, "humidity_pct": 90.0}, "ts": now},
                "in": {"m": {"temperature_c": 22.5, "humidity_pct": 44.0}, "ts": now}}
    off, _ = db.conditions(readings, cfg, now, None)               # sensor only: outdoor wetter → no boost
    assert db.decide(cfg, off, False, None, now)[0] is False
    on, _ = db.conditions(readings, cfg, now, (13.6, 67.0, now))    # cross-checked: outdoor ~7.6 vs house ~9.6
    want, why = db.decide(cfg, on, False, None, now)
    assert want is True and "Open-Meteo" in why


def test_policy_accepts_reference(tmp_path):
    import sqlite3
    from server.api.control import handle_policy_update
    from server.control import control_store as store
    c = sqlite3.connect(tmp_path / "c.db")
    store.init_db(c) if hasattr(store, "init_db") else store.ensure_schema(c)
    store.set_policy(c, "erv", {"enabled": True, "source_sensor": "x", "control": {"strategy": "hysteresis",
                     "on_above": 50, "off_below": 45}, "dry_boost": {"outdoor_sensor": "out", "indoor_sensors": ["in"]}})
    code, _ = handle_policy_update(c, "erv", {"dry_boost": {"reference": "openmeteo", "wet_rh": 80}})
    assert code == 200 and store.get_policy(c, "erv")["dry_boost"]["reference"] == "openmeteo"
    assert handle_policy_update(c, "erv", {"dry_boost": {"reference": "nws"}})[0] == 400
