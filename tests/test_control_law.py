"""Unified control law, stages L1–L2 (docs/design/unified-control-law.md): user bands → the cheapest combination of
actuators that holds the house in them, explained; shadow only (logs, never commands)."""
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


class Ctl:
    def __init__(self, function, device_type):
        self.function, self.device_type = function, device_type


REG = {"midea": Ctl("dehumidify", "dehumidifier"), "apr": Ctl("dehumidify", "dehum"), "erv": Ctl("ventilate", "erv"),
       "blower": Ctl("circulate_air", "hvac_fan"), "pur": Ctl("purify_air", "air_purifier"),
       "thermo": Ctl("condition_temperature", "thermostat")}
BANDS = B.BANDS_DEFAULT
KWH = B.COST_DEFAULT
PRM = M.params(None)


def _house(rh=44.0, t=22.0, w_out=None, t_out=10.0, aq=85.0, heating=False):
    w_in = S.ah(t, rh)
    return S.House(t, rh, t, w_in if w_out is None else w_out, t_out, aq, heating)


def _solve(h, current=None, cost=KWH, bands=BANDS):
    models = M.build(REG, PRM, {"w_in": h.w_in, "w_out": h.w_out, "t_in": h.t_in_c, "t_out": h.t_out_c})
    return S.solve(models, bands, cost, PRM, h, current or {})


def test_models_come_from_function_tags():
    kinds = {m.device_id: m.kind for m in M.build(REG, PRM, {})}
    assert kinds == {"midea": "midea", "apr": "aprilaire", "erv": "erv", "pur": "purifier", "thermo": "thermostat"}
    apr = next(m for m in M.build(REG, PRM, {}) if m.kind == "aprilaire")
    assert apr.needs == ("hvac_blower",) and apr.options[1].elec_w == 580 + 150


def test_in_band_runs_nothing_but_the_air_quality_ventilation():
    p = _solve(_house(rh=44.0, aq=85.0))
    assert p.choice["midea"]["level"] == 0 and p.choice["apr"]["level"] == 0
    assert p.choice["erv"]["level"] == 1                    # AQ above the floor: low
    assert p.need_gph == 0.0


def test_a_little_too_humid_picks_the_cheaper_dehumidifier():
    p = _solve(_house(rh=46.3))
    assert p.need_gph > 0
    assert p.choice["midea"]["level"] == 1                  # 1.04 L/kWh beats the Aprilaire's 0.75 (+ blower)
    assert p.choice["apr"]["level"] == 0


def test_very_humid_runs_both():
    p = _solve(_house(rh=52.0))
    assert p.choice["midea"]["level"] == 1 and p.choice["apr"]["level"] == 1


def test_drying_continues_to_the_bands_middle():
    h = _house(rh=44.0)
    assert _solve(h, {"midea": 1}).choice["midea"]["level"] == 1        # 44 > middle 43: keep drying
    assert _solve(h, {"midea": 0}).choice["midea"]["level"] == 0        # not drying: 44 is in band


def test_too_dry_no_dehumidifier_and_no_drying_ventilation_beyond_air_quality():
    p = _solve(_house(rh=38.0, w_out=4.0, aq=85.0))         # outdoor much drier: the ERV dries
    assert p.choice["midea"]["level"] == 0 and p.choice["apr"]["level"] == 0
    assert p.choice["erv"]["level"] == 0                    # off beats drying a dry house (AQ is only soft)


def test_drier_outdoor_air_is_the_cheapest_drying():
    p = _solve(_house(rh=46.1, w_out=3.0))                  # a small excess, very dry outdoor air
    assert p.need_gph > 0 and p.totals["firm"] == 0
    assert p.choice["midea"]["level"] == 0 and p.choice["apr"]["level"] == 0   # the ERV does it
    big = _solve(_house(rh=46.5, w_out=5.0))                # 436 g/h: more than the ERV at high (~290)
    assert big.choice["midea"]["level"] == 1


def test_poor_air_raises_ventilation_and_purifiers():
    p = _solve(_house(rh=44.0, aq=58.0))                    # 17 below the floor of 75, step 10
    assert p.choice["erv"]["level"] == 3 and p.choice["pur"]["level"] == 3


def test_money_basis_counts_dehumidifier_heat_against_gas_while_heating():
    cost = {"basis": "money", "electric_per_kwh": 0.40, "gas_per_therm": 2.00}
    h = _house(rh=46.3, heating=True)
    p = _solve(h, cost=cost)
    assert p.totals["cost_h"] < 0.40 * p.totals["elec_w"] / 1000      # the heat credit lowers the cost


def test_bands_validation():
    cur = {"bands": BANDS, "cost": KWH, "enrol": {}}
    assert B.validate({"bands": {"temp_f": {"min": 75, "max": 70}}}, cur)[0]
    assert B.validate({"bands": {"rh": {"min": 40, "max": 90}}}, cur)[0]
    assert B.validate({"cost": {"basis": "money"}}, cur)[0]                    # needs both rates
    assert "L4" in B.validate({"enrol": {"erv": "law"}}, cur)[0]               # not yet
    err, new = B.validate({"bands": {"rh": {"max": 47}}, "enrol": {"erv": "rule"}}, cur)
    assert err is None and new["bands"]["rh"] == {"min": 40.0, "max": 47} and new["enrol"] == {"erv": "rule"}


def test_shadow_logs_and_holds(tmp_path):
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    sh = SH.Shadow(store)
    h = _house(rh=46.3)
    p1 = sh.run(conn, NOW, REG, h, {"midea": 0, "apr": 0, "erv": 1})
    assert p1.choice["midea"]["level"] == 1
    row = conn.execute("SELECT plan, actual FROM law_shadow").fetchone()
    assert json.loads(row[0])["midea"]["level"] == 1 and json.loads(row[1])["midea"] == 0
    p2 = sh.run(conn, NOW + 300, REG, _house(rh=43.0), {"midea": 1})     # inside band now, but held 15 min
    last = store.get_setting(conn, "law:last")
    assert last["choice"]["midea"]["level"] == 1 and last["choice"]["midea"]["held_s"] > 0
    B.put(conn, store, {"enrol": {"midea": "rule"}})
    sh.run(conn, NOW + 2000, REG, h, {"midea": 0})
    plan = json.loads(conn.execute("SELECT plan FROM law_shadow ORDER BY ts DESC").fetchone()[0])
    assert "midea" not in plan                                # rule-enrolled: left out of the comparison
    v = SH.view(conn, store, NOW + 2000)
    assert v["agreement_24h"]["apr"]["ticks"] >= 1


class Iss:
    def __init__(self):
        self.calls = []

    def issue(self, *, device_id, trait, action, args, **kw):
        self.calls.append((device_id, trait, args))
        return Result("ok", "ok", intended=args, reported=args)


def test_controller_runs_the_shadow_and_commands_nothing_new(tmp_path):
    db = str(tmp_path / "control.db")
    conn = sqlite3.connect(db)
    store.ensure_schema(conn)
    conn.close()
    reg = parse_control_registry(yaml.safe_load(open("config-examples/control.example.yaml")))
    iss = Iss()
    ctrl = C.Controller(iss, {}, reg, db)
    for sid in B.ROOMS:
        ctrl.readings[sid] = {"m": {"temperature_c": 21.5, "humidity_pct": 47.0}, "ts": NOW - 30}
    ctrl.tick(now=NOW)
    conn = sqlite3.connect(db)
    last = store.get_setting(conn, "law:last")
    assert last is not None and any("RH 47.0" in r for r in last["reasons"])
    assert iss.calls == []                                    # no policies: the law alone commands nothing
