"""Unified law stage L4 (docs/design/unified-control-law.md §6): a device enrolled `law` is driven by the law's
held answer, below safety / overrides / scenes / schedules / the study, with a fallback to its own rule when the
law has no fresh decision. Thermostats enrolled `law` are held at the temperature band's setpoints."""
import json
import sqlite3

import pytest

from server.control import control_store as store
from server.control import controller as C
from server.control.issuer import Result
from server.control.law import bands as B
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
