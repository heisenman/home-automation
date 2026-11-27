"""Unified law stage L4 (docs/design/unified-control-law.md §6): a device enrolled `law` is driven by the law's
held answer, below safety / overrides / scenes / schedules / the study, with a fallback to its own rule when the
law has no fresh decision. Thermostats enrolled `law` are held at the temperature band's setpoints."""
import json
import sqlite3

import pytest

from server.control import control_store as store
from server.control import controller as C
from server.control.issuer import Result
from server.control import manual_restore as MR
from server.control.law import bands as B
from server.control.law import holds as H
from server.control.law import modes as MO
from server.control.law import models as M
from server.control.law import shadow as SH
from server.control.law import solve as S
from server.control.registry import parse_control_registry

yaml = pytest.importorskip("yaml")
NOW = 2_000_000.0
ERV_POL = {"enabled": True, "source_sensor": "gas_x",
           "control": {"strategy": "threshold_ranged", "metric": "air_quality",
                       "bands": [{"max": 60, "level": 3}, {"max": 75, "level": 2}, {"max": None, "level": 1}]}}
DEHUM_POL = {"enabled": True, "source_sensor": "meter_x",
             "control": {"strategy": "hysteresis", "on_above": 48, "off_below": 45, "min_on_min": 10,
                         "min_off_min": 10}}


class Iss:
    def __init__(self):
        self.calls = []

    def issue(self, *, device_id, trait, action, args, **kw):
        self.calls.append((device_id, trait, args))
        return Result("ok", "ok", intended=args, reported=args)


def _ctrl(tmp_path, enrol=None, pols=None):
    db = str(tmp_path / "control.db")
    conn = sqlite3.connect(db)
    store.ensure_schema(conn)
    for d, p in (pols or {}).items():
        store.set_policy(conn, d, p)
    if enrol:
        assert B.put(conn, store, {"enrol": enrol})[0] == 200
    conn.close()
    reg = parse_control_registry(yaml.safe_load(open("config-examples/control.example.yaml")))
    iss = Iss()
    ctrl = C.Controller(iss, {}, reg, db)
    ctrl._law.last_run = NOW + 10_000                  # the law doesn't re-decide inside these ticks
    return ctrl, iss, db


def _decide(ctrl, at, **levels):
    ctrl._law.choice = {d: {"level": v, "label": f"level {v}"} for d, v in levels.items()}
    ctrl._law.decided_at = at


def _log(db, device_id):
    c = sqlite3.connect(db)
    return c.execute("SELECT source, reason, acted FROM control_log WHERE device_id=? ORDER BY rowid DESC LIMIT 1",
                     (device_id,)).fetchone()


# ── settings ─────────────────────────────────────────────────────────────────────────────────────

def test_law_is_an_enrolment_mode_and_unknown_devices_are_refused():
    cur = {"bands": B.BANDS_DEFAULT, "cost": B.COST_DEFAULT, "enrol": {}}
    assert B.validate({"enrol": {"erv": "law"}}, cur)[0] is None
    assert B.validate({"enrol": {"erv": "auto"}}, cur)[0]
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    code, body = B.put(conn, store, {"enrol": {"nope": "law"}}, devices=["erv_attic"])
    assert code == 400 and "nope" in body["reason"]
    assert B.put(conn, store, {"enrol": {"erv_attic": "law"}}, devices=["erv_attic"])[0] == 200


def test_drying_starts_at_the_band_top_like_the_rule():
    # 11-26 05:12: RH 46.0 on the band top; the Midea's rule (on_above 46, >=) started it, the law waited for > 46
    reg = {"midea": type("X", (), {"function": "dehumidify", "device_type": "dehumidifier"})()}
    h = S.House(22.0, 46.0, 22.0, S.ah(22.0, 46.0), 10.0, 85.0)
    prm = M.params(None)
    models = M.build(reg, prm, {"w_in": h.w_in, "w_out": h.w_out, "t_in": 22.0, "t_out": 10.0})
    p = S.solve(models, B.BANDS_DEFAULT, B.COST_DEFAULT, prm, h, {"midea": 0})
    assert p.need_gph > 0 and p.choice["midea"]["level"] == 1
    h2 = S.House(22.0, 45.9, 22.0, S.ah(22.0, 45.9), 10.0, 85.0)
    assert S.solve(models, B.BANDS_DEFAULT, B.COST_DEFAULT, prm, h2, {"midea": 0}).choice["midea"]["level"] == 0


# ── controller: level-mode (ERV) ─────────────────────────────────────────────────────────────────

def _erv_tel(ctrl, fan=1, fan_mode=9):
    ctrl.telemetry["erv_attic"] = {"running": True, "fan": fan, "fan_mode": fan_mode, "ts": NOW}


def test_law_drives_an_enrolled_erv(tmp_path):
    ctrl, iss, db = _ctrl(tmp_path, {"erv_attic": "law"}, {"erv_attic": ERV_POL})
    _erv_tel(ctrl, fan=1, fan_mode=9)                  # low
    _decide(ctrl, NOW - 60, erv_attic=3)
    ctrl.tick(now=NOW)
    assert ("erv_attic", "mode", {"mode": "high"}) in iss.calls
    src, why, acted = _log(db, "erv_attic")
    assert src == "law" and why.startswith("law -> high") and acted
    assert ctrl._law.rule_said["erv_attic"] == 1       # its own rule (sensor missing -> default hold at low)


def test_shadow_enrolled_erv_is_not_driven(tmp_path):
    ctrl, iss, db = _ctrl(tmp_path, {"erv_attic": "shadow"}, {"erv_attic": ERV_POL})
    _erv_tel(ctrl)
    _decide(ctrl, NOW - 60, erv_attic=3)
    ctrl.tick(now=NOW)
    assert not [c for c in iss.calls if c[0] == "erv_attic"]
    assert _log(db, "erv_attic")[0] != "law"


def test_override_beats_the_law(tmp_path):
    ctrl, iss, db = _ctrl(tmp_path, {"erv_attic": "law"}, {"erv_attic": ERV_POL})
    conn = sqlite3.connect(db)
    store.set_override(conn, "erv_attic", "off", NOW + 3600)
    conn.close()
    _erv_tel(ctrl)
    _decide(ctrl, NOW - 60, erv_attic=3)
    ctrl.tick(now=NOW)
    assert ("erv_attic", "mode", {"mode": "off"}) in iss.calls
    assert _log(db, "erv_attic")[0] == "override"


def test_no_decision_holds_after_start_then_falls_back_to_the_rule(tmp_path):
    ctrl, iss, db = _ctrl(tmp_path, {"erv_attic": "law"}, {"erv_attic": ERV_POL})
    _erv_tel(ctrl)
    ctrl.tick(now=NOW)                                 # first tick: no decision yet -> hold
    assert _log(db, "erv_attic")[:2] == ("law", "law: no decision yet since start -> hold")
    _decide(ctrl, NOW, erv_attic=2)
    late = NOW + SH.STALE_S + 60                       # the law stopped deciding
    _erv_tel(ctrl)
    ctrl.telemetry["erv_attic"]["ts"] = late
    ctrl.tick(now=late)
    src, why, _ = _log(db, "erv_attic")
    assert src != "law" and "law decision stale -> own rule" in why


# ── controller: ranged (purifier) + leased call (Aprilaire) ──────────────────────────────────────

def test_law_drives_a_hysteresis_dehumidifier_with_its_min_off(tmp_path):
    ctrl, iss, db = _ctrl(tmp_path, {"dehum_attic": "law", "erv_attic": "rule"}, {"dehum_attic": DEHUM_POL})
    ctrl.telemetry["dehum_attic"] = {"running": False, "fan": None, "lease_left_s": None, "force": None, "ts": NOW}
    _erv_tel(ctrl)                                     # moving air: the airflow interlock allows it
    conn = sqlite3.connect(db)
    store.record_transition(conn, "dehum_attic", False, NOW - 120)   # stopped 2 min ago, min-off 10
    conn.close()
    _decide(ctrl, NOW - 60, dehum_attic=1)
    ctrl.tick(now=NOW)
    src, why, acted = _log(db, "dehum_attic")
    assert src == "law" and "min-off" in why and not acted
    ctrl.tick(now=NOW + 600)
    assert ("dehum_attic", "switchable", {"on": True}) in iss.calls
    assert _log(db, "dehum_attic")[0] == "law"


def test_the_shadow_row_logs_the_rule_for_law_devices():
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    B.put(conn, store, {"enrol": {"erv": "law"}})
    reg = {"erv": type("X", (), {"function": "ventilate", "device_type": "erv"})()}
    sh = SH.Shadow(store)
    sh.rule_said["erv"] = 2
    h = S.House(22.0, 44.0, 22.0, S.ah(22.0, 44.0), 10.0, 85.0)
    sh.run(conn, NOW, reg, h, {"erv": 1})
    assert json.loads(conn.execute("SELECT rule FROM law_shadow").fetchone()[0]) == {"erv": 2}
    assert sh.decision("erv", NOW + 60)["level"] == 1
    assert sh.decision("erv", NOW + SH.STALE_S + 1) is None
    v = SH.view(conn, store, NOW + 60)
    assert v["rule_agreement_24h"]["erv"] == {"ticks": 1, "same": 0}


def test_old_law_shadow_table_gains_the_rule_column():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE law_shadow (ts REAL NOT NULL, plan TEXT, actual TEXT, errors TEXT, need_gph REAL, "
                 "totals TEXT, reasons TEXT, house TEXT)")
    SH.ensure_schema(conn)
    assert "rule" in {r[1] for r in conn.execute("PRAGMA table_info(law_shadow)")}


# ── thermostats ──────────────────────────────────────────────────────────────────────────────────

def _thermo(ctrl, heat_f, cool_f, at=NOW, **extra):
    ctrl.readings["thermostat_hall"] = {"m": {"heat_setpoint_c": S.f2c(heat_f), "cool_setpoint_c": S.f2c(cool_f),
                                              "hvac_mode": 1, **extra}, "ts": at}


def test_law_holds_an_enrolled_thermostat_at_the_band(tmp_path):
    ctrl, iss, db = _ctrl(tmp_path, {"thermostat_hall": "law"})
    _thermo(ctrl, 74, 78)
    _decide(ctrl, NOW - 60, thermostat_hall=None)
    ctrl._law.choice["thermostat_hall"] = {"level": None, "label": "heat to 68 °F, cool above 76 °F"}
    ctrl.tick(now=NOW)
    assert [c for c in iss.calls if c[0] == "thermostat_hall"] == [
        ("thermostat_hall", "setpoint", {"channel": "heat", "value": 68.0}),
        ("thermostat_hall", "setpoint", {"channel": "cool", "value": 76.0})]
    ctrl.tick(now=NOW + 60)                            # the wall still reads the old values: no re-write for 10 min
    assert len([c for c in iss.calls if c[0] == "thermostat_hall"]) == 2


def test_law_leaves_the_thermostat_alone_when_its_schedule_is_on_or_not_enrolled(tmp_path):
    ctrl, iss, db = _ctrl(tmp_path, {"thermostat_hall": "law"})
    _thermo(ctrl, 74, 78, schedule_on=1)
    ctrl._law.choice, ctrl._law.decided_at = {"thermostat_hall": {"level": None, "label": "x"}}, NOW - 60
    ctrl.tick(now=NOW)
    ctrl.tick(now=NOW + 60)
    assert not iss.calls
    c = sqlite3.connect(db)
    rows = c.execute("SELECT reason FROM control_log WHERE device_id='thermostat_hall'").fetchall()
    assert rows == [("law: the thermostat's own schedule/away is on -> hold",)]   # logged once, not per tick
    ctrl2, iss2, _ = _ctrl(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path)
    _thermo(ctrl2, 74, 78)
    ctrl2._law.choice, ctrl2._law.decided_at = {"thermostat_hall": {"level": None, "label": "x"}}, NOW - 60
    ctrl2.tick(now=NOW)
    assert not iss2.calls                              # default enrolment is shadow: never touched


# ── goal schedules ───────────────────────────────────────────────────────────────────────────────

NIGHT = {"name": "Night", "window": "22:00-07:00", "temp_f": {"min": 64, "max": 72}}


def test_goal_schedule_replaces_only_the_goals_it_names_and_wraps_midnight():
    bands = {**B.BANDS_DEFAULT, "schedules": [{**NIGHT, "days": [4]}]}          # Friday nights
    eff, name = B.effective(bands, 23 * 60, 4)
    assert name == "Night" and eff["temp_f"] == {"min": 64, "max": 72} and eff["rh"] == B.BANDS_DEFAULT["rh"]
    assert B.effective(bands, 2 * 60, 5)[1] == "Night"                          # Saturday 02:00: Friday's night
    assert B.effective(bands, 2 * 60, 4)[1] is None                             # Friday 02:00: Thursday's night
    assert B.effective(bands, 12 * 60, 4)[1] is None
    off = {**B.BANDS_DEFAULT, "schedules": [{**NIGHT, "enabled": False}]}
    assert B.effective(off, 23 * 60, 4)[1] is None


def test_goal_schedule_validation():
    cur = {"bands": B.BANDS_DEFAULT, "cost": B.COST_DEFAULT, "enrol": {}}
    ok = lambda sc: B.validate({"bands": {"schedules": sc}}, cur)[0]
    assert ok([NIGHT]) is None
    assert ok([{**NIGHT, "window": "25:00-07:00"}])
    assert ok([{**NIGHT, "window": "07:00-07:00"}])
    assert ok([{"name": "x", "window": "01:00-02:00"}])                         # sets no goal
    assert ok([{**NIGHT, "temp_f": {"min": 75, "max": 70}}])
    assert ok([{**NIGHT, "days": [7]}])
    assert ok([{**NIGHT, "aq": {"floor": 70, "weight": 3}}])                    # only the floor is scheduled
    assert ok([NIGHT] * 13)
    assert B.validate({"bands": {"hold_h": 0}}, cur)[0]


def test_next_change_finds_the_schedule_edge():
    bands = {**B.BANDS_DEFAULT, "schedules": [{**NIGHT, "window": "22:00-07:00"}]}
    tz = "UTC"
    import calendar
    t = calendar.timegm((2026, 11, 26, 21, 30, 0))
    assert B.next_change(bands, t, tz) == calendar.timegm((2026, 11, 26, 22, 0, 0))
    assert B.next_change(B.BANDS_DEFAULT, t, tz) is None


def test_the_law_solves_on_the_scheduled_goals():
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    store.set_setting(conn, "timezone", "UTC")
    B.put(conn, store, {"bands": {"schedules": [{"name": "Dry", "window": "00:00-23:59", "rh": {"min": 35, "max": 40}}]}})
    reg = {"midea": type("X", (), {"function": "dehumidify", "device_type": "dehumidifier"})()}
    h = S.House(22.0, 43.0, 22.0, S.ah(22.0, 43.0), 10.0, 85.0)                 # in the base band, above Dry's
    p = SH.Shadow(store).run(conn, NOW, reg, h, {"midea": 0})
    assert p.choice["midea"]["level"] == 1 and p.reasons[0] == "goals: the Dry schedule"
    assert store.get_setting(conn, "law:last")["schedule"] == "Dry"


# ── holds ────────────────────────────────────────────────────────────────────────────────────────

def test_a_pwa_command_on_a_law_device_places_a_hold_that_ends_at_the_schedule_edge():
    import calendar
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    store.set_setting(conn, "timezone", "UTC")
    t = calendar.timegm((2026, 11, 26, 21, 30, 0))
    assert H.on_manual_command(conn, store, "erv_attic", {"trait": "mode", "args": {"mode": "high"}}, t) is None
    B.put(conn, store, {"enrol": {"erv_attic": "law"}, "bands": {"schedules": [NIGHT]}})
    h = H.on_manual_command(conn, store, "erv_attic", {"trait": "mode", "args": {"mode": "high"}}, t)
    assert h["by"] == "pwa" and h["until"] == calendar.timegm((2026, 11, 26, 22, 0, 0))   # before hold_h's 2 h
    assert H.active(conn, store, "erv_attic", t + 60) is not None
    assert H.active(conn, store, "erv_attic", h["until"] + 1) is None                       # expired, cleared
    assert H.all_active(conn, t) == {}


def test_a_hold_makes_the_law_yield(tmp_path):
    ctrl, iss, db = _ctrl(tmp_path, {"erv_attic": "law"}, {"erv_attic": ERV_POL})
    conn = sqlite3.connect(db)
    H.place(conn, store, "erv_attic", "pwa", NOW - 60)
    conn.close()
    _erv_tel(ctrl, fan=3, fan_mode=10)                 # a person set high
    _decide(ctrl, NOW - 60, erv_attic=1)
    ctrl.tick(now=NOW)
    assert not [c for c in iss.calls if c[0] == "erv_attic"]
    src, why, _ = _log(db, "erv_attic")
    assert src == "law" and why.startswith("manual hold (set from the PWA) until")


def test_a_setpoint_changed_at_the_wall_becomes_a_hold(tmp_path):
    ctrl, iss, db = _ctrl(tmp_path, {"thermostat_hall": "law"})
    ctrl._law.choice, ctrl._law.decided_at = {"thermostat_hall": {"level": None, "label": "x"}}, NOW - 60
    _thermo(ctrl, 74, 78)
    ctrl.tick(now=NOW)                                 # law writes 68 / 76
    _thermo(ctrl, 68, 76, at=NOW + 30)
    ctrl.tick(now=NOW + 30)                            # our own write read back: not a wall change
    c = sqlite3.connect(db)
    assert H.active(c, store, "thermostat_hall", NOW + 30) is None
    _thermo(ctrl, 71, 76, at=NOW + 900)                # someone turned it up at the wall
    n = len(iss.calls)
    ctrl.tick(now=NOW + 900)
    h = H.active(c, store, "thermostat_hall", NOW + 900)
    assert h["by"] == "wall" and h["detail"] == {"heat": 71.0} and len(iss.calls) == n
    assert c.execute("SELECT count(*) FROM control_log WHERE reason LIKE 'law: set at the thermostat (heat 71 °F)%'"
                     ).fetchone()[0] == 1
    H.clear(c, store, "thermostat_hall")               # "resume law"
    ctrl._law.decided_at = NOW + 1500
    ctrl.tick(now=NOW + 1600)
    assert iss.calls[-1] == ("thermostat_hall", "setpoint", {"channel": "heat", "value": 68.0})


# ── modes (Manual / Own automation / Law) ────────────────────────────────────────────────────────

def test_modes_map_onto_policy_enabled_and_enrolment():
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    store.set_policy(conn, "erv_attic", ERV_POL)
    assert MO.of(conn, store, "erv_attic") == "auto" and MO.of(conn, store, "thermostat_hall") == "manual"
    assert MO.apply(conn, store, {"erv_attic": "law", "thermostat_hall": "law"})[0] == 200
    assert MO.of(conn, store, "erv_attic") == "law" and MO.of(conn, store, "thermostat_hall") == "law"
    assert MO.apply(conn, store, {"erv_attic": "manual"})[0] == 200
    assert store.get_policy(conn, "erv_attic")["enabled"] is False and MO.of(conn, store, "erv_attic") == "manual"
    assert B.get(conn, store)["enrol"]["erv_attic"] == "shadow"
    assert MO.apply(conn, store, {"erv_attic": "auto"})[0] == 200
    assert store.get_policy(conn, "erv_attic")["enabled"] is True
    assert MO.apply(conn, store, {"erv_attic": "robot"})[0] == 400
    assert MO.apply(conn, store, {"ghost": "law"}, devices=["erv_attic"])[0] == 400


# ── Manual devices come back after a power loss ──────────────────────────────────────────────────

def test_manual_restore_decisions():
    on2 = {"running": True, "level": 2, "mode": None}
    off = {"running": False, "level": None, "mode": None}
    assert MR.decide(on2, on2, None, 0, 0, NOW)[0] == "none"
    assert MR.decide(on2, off, None, 0, 0, NOW)[0] == "remember"        # a person turned it off: that's the setting
    assert MR.decide(on2, off, NOW - 10, 0, 0, NOW)[0] == "wait"        # just back
    assert MR.decide(on2, off, NOW - 90, 0, 0, NOW)[0] == "restore"
    assert MR.decide(on2, off, NOW - 120, 1, NOW - 30, NOW)[0] == "wait"
    assert MR.decide(on2, off, NOW - 200, 3, NOW - 90, NOW)[0] == "give_up"
    assert MR.decide(on2, on2, NOW - 90, 1, NOW - 60, NOW)[0] == "done"



def test_a_manual_ranged_device_is_put_back_after_being_offline(tmp_path):
    pol = {"enabled": False, "source_sensor": "gas_x",
           "control": {"strategy": "threshold_ranged", "metric": "air_quality",
                       "bands": [{"max": None, "level": 1}]}}
    ctrl, iss, db = _ctrl(tmp_path, pols={"lamp_office": pol})        # any disabled-policy telemetry device
    tel = lambda run, fan, at: ctrl.telemetry.__setitem__("lamp_office", {"running": run, "fan": fan, "ts": at})
    tel(True, 2, NOW)
    ctrl.tick(now=NOW)                                 # start counts as a return; same as nothing remembered -> done
    ctrl.tick(now=NOW + 30)                            # remembered: on at 2
    c = sqlite3.connect(db)
    assert store.get_setting(c, MR.key("lamp_office"))["level"] == 2
    ctrl._avail["lamp_office"] = (False, NOW + 60)     # brownout: the LWT says offline
    ctrl.tick(now=NOW + 60)
    ctrl._avail["lamp_office"] = (True, NOW + 120)     # back, but OFF
    tel(False, None, NOW + 120)
    ctrl.tick(now=NOW + 120)                           # settling
    assert not iss.calls
    tel(False, None, NOW + 200)
    ctrl.tick(now=NOW + 200)
    assert iss.calls[:2] == [("lamp_office", "switchable", {"on": True}), ("lamp_office", "ranged", {"level": 2})]
    tel(True, 2, NOW + 260)
    ctrl.tick(now=NOW + 260)
    assert c.execute("SELECT reason FROM control_log WHERE device_id='lamp_office' ORDER BY rowid DESC LIMIT 1"
                     ).fetchone()[0] == "manual: restored (on at level 2)"
    tel(False, None, NOW + 400)                        # turned off by a person while online: respected
    ctrl.tick(now=NOW + 400)
    ctrl.tick(now=NOW + 600)
    assert len(iss.calls) == 2 and store.get_setting(c, MR.key("lamp_office"))["running"] is False


# ── thermostat setpoints carry the hall-vs-rooms offset (law/thermo.py; Hugh 2026-11-27) ──────────────────────

from server.control.law import thermo as T


def test_offset_setpoints_round_heat_up_and_cool_down():
    sp, off = T.setpoints(70, 74, 2.5, {})                  # 11-27 07:30: the hall 72.0, rooms 69.5
    assert sp == {"heat": 73.0, "cool": 76.0} and off == 2.5
    assert T.setpoints(70, 74, None, {})[0] == {"heat": 70.0, "cool": 74.0}        # no rooms: the band itself
    assert T.setpoints(70, 74, 2.5, {"enabled": False})[0] == {"heat": 70.0, "cool": 74.0}
    assert T.setpoints(70, 74, 9.0, {})[0]["heat"] == 74.0                         # capped at +4
    assert T.setpoints(70, 74, -1.5, {})[0] == {"heat": 69.0, "cool": 72.0}        # hall cooler than the rooms
    assert T.setpoints(70, 71, 0.0, {})[0] == {"heat": 70.0, "cool": 72.0}         # cool stays 2 above heat


def test_offset_setpoint_holds_across_a_degree_edge():
    assert T.setpoints(70, 74, 2.1, {}, have_heat=73)[0]["heat"] == 73.0     # raw 72.1: ceil says 73 anyway
    assert T.setpoints(70, 74, 1.9, {}, have_heat=73)[0]["heat"] == 73.0     # raw 71.9: within the slack, hold
    assert T.setpoints(70, 74, 1.6, {}, have_heat=73)[0]["heat"] == 72.0     # raw 71.6: past it, move
    assert T.setpoints(70, 74, 3.2, {}, have_heat=73)[0]["heat"] == 73.0     # raw 73.2: hold
    assert T.setpoints(70, 74, 3.4, {}, have_heat=73)[0]["heat"] == 74.0


def test_offset_is_smoothed():
    assert T.smooth(None, None, 2.5, NOW, 30) == 2.5
    v = T.smooth(0.0, NOW - 1800, 3.0, NOW, 30)              # one time constant: ~63 % of the way
    assert 1.8 < v < 2.0


def test_heat_to_and_cool_to_are_written_as_set_whatever_the_rooms_read(tmp_path):
    # Hugh 2026-11-27: "make heat to / cool to explicit … set 74 as my heating setpoint" — no offset by default
    ctrl, iss, db = _ctrl(tmp_path, {"thermostat_hall": "law"})
    c = sqlite3.connect(db)
    assert B.put(c, store, {"bands": {"temp_f": {"min": 74, "max": 76}}})[0] == 200
    for sid in B.ROOMS:
        ctrl.readings[sid] = {"m": {"temperature_c": S.f2c(69.5)}, "ts": NOW - 30}
    _thermo(ctrl, 70, 74, temperature_c=S.f2c(72.0))
    _decide(ctrl, NOW - 60, thermostat_hall=None)
    ctrl.tick(now=NOW)
    assert [cl for cl in iss.calls if cl[0] == "thermostat_hall"] == [
        ("thermostat_hall", "setpoint", {"channel": "cool", "value": 76.0}),
        ("thermostat_hall", "setpoint", {"channel": "heat", "value": 74.0})]
    why = c.execute("SELECT reason FROM control_log WHERE device_id='thermostat_hall' ORDER BY rowid DESC LIMIT 1"
                    ).fetchone()[0]
    assert why == "law: heat setpoint 70 -> 74 °F (heat to 74 / cool to 76 °F)"
    t = store.get_setting(c, T.KEY_PREFIX + "thermostat_hall")
    assert (t["heat"], t["cool"], t["offset_f"], t["stat_f"], t["rooms_f"]) == (74.0, 76.0, 0.0, 72.0, 69.5)


def test_heat_to_and_cool_to_are_whole_degrees_two_apart():
    cur = {"bands": B.BANDS_DEFAULT, "cost": B.COST_DEFAULT, "enrol": {}}
    assert B.validate({"bands": {"temp_f": {"min": 74, "max": 76}}}, cur)[0] is None
    assert "2 °F" in B.validate({"bands": {"temp_f": {"min": 74, "max": 75}}}, cur)[0]
    assert "whole" in B.validate({"bands": {"temp_f": {"min": 73.5, "max": 76}}}, cur)[0]
    bad = {"bands": {"schedules": [{"name": "N", "window": "21:00-06:00", "enabled": True,
                                    "temp_f": {"min": 70, "max": 71}}]}}
    assert "2 °F" in B.validate(bad, cur)[0]


def test_law_sets_the_thermostat_so_the_rooms_reach_the_band(tmp_path):
    # the optional offset (off by default since Hugh's explicit heat-to / cool-to)
    ctrl, iss, db = _ctrl(tmp_path, {"thermostat_hall": "law"})
    c = sqlite3.connect(db)
    assert B.put(c, store, {"bands": {"temp_f": {"min": 70, "max": 74},
                                      "thermostat_offset": {"enabled": True}}})[0] == 200
    for sid in B.ROOMS:
        ctrl.readings[sid] = {"m": {"temperature_c": S.f2c(69.5)}, "ts": NOW - 30}
    _thermo(ctrl, 70, 74, temperature_c=S.f2c(72.0))
    _decide(ctrl, NOW - 60, thermostat_hall=None)
    ctrl.tick(now=NOW)
    assert [cl for cl in iss.calls if cl[0] == "thermostat_hall"] == [
        ("thermostat_hall", "setpoint", {"channel": "cool", "value": 76.0}),      # raising: cool first
        ("thermostat_hall", "setpoint", {"channel": "heat", "value": 73.0})]
    why = c.execute("SELECT reason FROM control_log WHERE device_id='thermostat_hall' ORDER BY rowid DESC LIMIT 1"
                    ).fetchone()[0]
    assert "heat setpoint 70 -> 73 °F (heat to 70 / cool to 74 °F" in why and "reads 2.5 °F warmer than the rooms" in why
    _thermo(ctrl, 73, 76, temperature_c=S.f2c(72.0))       # took it; our own write is not a wall change
    ctrl.tick(now=NOW + 700)
    assert len([cl for cl in iss.calls if cl[0] == "thermostat_hall"]) == 2 and H.active(c, store, "thermostat_hall", NOW + 700) is None


def test_the_comfort_panel_sees_what_the_thermostat_is_set_to_and_why(tmp_path):
    ctrl, iss, db = _ctrl(tmp_path, {"thermostat_hall": "law"})
    c = sqlite3.connect(db)
    B.put(c, store, {"bands": {"temp_f": {"min": 70, "max": 74}, "thermostat_offset": {"enabled": True}}})
    for sid in B.ROOMS:
        ctrl.readings[sid] = {"m": {"temperature_c": S.f2c(69.5)}, "ts": NOW - 30}
    _thermo(ctrl, 70, 74, temperature_c=S.f2c(72.0))
    _decide(ctrl, NOW - 60, thermostat_hall=None)
    ctrl.tick(now=NOW)
    t = store.get_setting(c, T.KEY_PREFIX + "thermostat_hall")
    assert (t["heat"], t["cool"], t["offset_f"], t["rooms_f"], t["stat_f"]) == (73.0, 76.0, 2.5, 69.5, 72.0)
    assert t["set_heat"] == 70 and t["set_cool"] == 74
