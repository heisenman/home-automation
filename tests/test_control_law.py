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
MONEY = {"basis": "money", "electric_per_kwh": 0.18, "gas_per_therm": 1.6}


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
    p = _solve(_house(rh=60.0))
    assert p.choice["midea"]["level"] == 1 and p.choice["apr"]["level"] == 1


def test_both_dehumidifiers_remove_less_than_the_sum():
    # Phase 4 §8: run together they dry the same air; the house sees only `with_aprilaire` of the Midea's water
    p = _solve(_house(rh=60.0))                             # outdoor = indoor AH: the ERV moves no water
    mid, apr = (next(m for m in M.build(REG, PRM, {}) if m.kind == k).options[1].water_gph for k in ("midea", "aprilaire"))
    assert p.totals["water_gph"] == pytest.approx(apr + mid * PRM["midea"]["with_aprilaire"], abs=0.2)


def test_drying_continues_to_the_bands_middle():
    h = _house(rh=44.0)
    assert _solve(h, {"midea": 1}).choice["midea"]["level"] == 1        # 44 > middle 43: keep drying
    assert _solve(h, {"midea": 0}).choice["midea"]["level"] == 0        # not drying: 44 is in band


def test_too_dry_no_dehumidifier_and_no_drying_ventilation_beyond_air_quality():
    p = _solve(_house(rh=38.0, w_out=4.0, aq=85.0))         # outdoor much drier: the ERV dries
    assert p.choice["midea"]["level"] == 0 and p.choice["apr"]["level"] == 0
    assert p.choice["erv"]["level"] == 1                    # the AQ level stays: air quality trumps (11-21)
    poor = _solve(_house(rh=38.0, w_out=4.0, aq=58.0))      # 11-21 overnight: 39 %, AQ 58
    assert poor.choice["erv"]["level"] == 3 and poor.totals["firm"] == 0


def test_drier_outdoor_air_is_the_cheapest_drying():
    p = _solve(_house(rh=46.1, w_out=3.0))                  # a small excess, very dry outdoor air
    assert p.need_gph > 0 and p.totals["firm"] == 0
    assert p.choice["midea"]["level"] == 0 and p.choice["apr"]["level"] == 0   # the ERV does it
    big = _solve(_house(rh=50.0, w_out=7.5))                # ~300 g/h: more than the ERV at high (~180)
    assert big.choice["midea"]["level"] == 1


def test_in_band_drier_outdoor_air_runs_the_ventilator_high():
    h = _house(rh=43.0, t=22.0, w_out=5.7, t_out=7.0, aq=85.0)   # dew point ~9 in / ~2.5 out (Hugh 2026-11-17)
    p = _solve(h, cost=MONEY)
    assert p.need_gph == 0.0 and p.choice["erv"]["level"] == 3
    assert p.choice["midea"]["level"] == 0 and p.choice["apr"]["level"] == 0
    assert any("to dry" in r for r in p.reasons)


def test_outdoor_drying_has_hysteresis_and_stops_below_the_band():
    bands = {**BANDS, "outdoor_drying": {**BANDS["outdoor_drying"], "cost_check": False}}   # the dew-point rule alone

    def _solve(h, current=None, cost=KWH, bands=bands):
        return globals()["_solve"](h, current, cost, bands)
    gap1 = _house(rh=43.0, w_out=S.ah_dp(S.dp_ah(S.ah(22.0, 43.0), 22.0) - 1.0, 10.0))   # outdoor 1 °C drier
    assert _solve(gap1, {"erv": 1}).choice["erv"]["level"] == 1           # not started at a 1 °C gap
    assert _solve(gap1, {"erv": 3}).choice["erv"]["level"] == 3           # but held once running (off at 0.75)
    assert _solve(_house(rh=40.0, w_out=5.0), {"erv": 3}).choice["erv"]["level"] == 1   # at the floor: stops
    assert _solve(_house(rh=41.0, w_out=5.0), {"erv": 1}).choice["erv"]["level"] == 1   # not back above 41 yet
    assert _solve(_house(rh=41.0, w_out=5.0), {"erv": 3}).choice["erv"]["level"] == 3   # held while above 40
    assert _solve(_house(rh=42.0, w_out=5.0), {"erv": 1}).choice["erv"]["level"] == 3   # restarted
    dry = _house(rh=39.0, w_out=4.0, aq=85.0)
    assert _solve(dry, {"erv": 3}).choice["erv"]["level"] == 1            # below the band: back to the AQ level
    off = {**BANDS, "outdoor_drying": {**BANDS["outdoor_drying"], "enabled": False}}
    assert _solve(_house(rh=43.0, w_out=5.0), bands=off).choice["erv"]["level"] == 1


def test_in_band_drying_must_be_worth_its_fan_and_heat_cost():
    # 11-20 03:22 PST: RH 41 in band, outdoor 3.6 °C and much drier, heating — Midea water (heat credited against
    # gas) is far cheaper than the ERV's extra fan + heat loss at high, so the law stays at the AQ level
    cold = S.House(22.2, 43.0, 22.3, 6.05, 3.6, 85.0, heating=True)
    p = _solve(cold, {"erv": 3}, cost=MONEY)
    assert p.choice["erv"]["level"] == 1
    assert any("not worth it" in r for r in p.reasons)
    # not heating, mild, very dry outdoor air: the fan alone is cheap enough against the dehumidifier's electricity
    mild = S.House(22.0, 43.0, 22.0, 3.5, 16.0, 85.0)
    p2 = _solve(mild, {"erv": 1}, cost=MONEY)
    assert p2.choice["erv"]["level"] == 3 and any("to dry" in r and "worth" in r for r in p2.reasons)
    # above the band the water need prices drying itself: no check
    wet = S.House(22.2, 47.0, 22.3, 4.0, 3.6, 85.0, heating=True)
    assert not any("not worth it" in r for r in _solve(wet, {"erv": 3}, cost=MONEY).reasons)


def test_drying_worth_check_has_hysteresis():
    models = M.build(REG, PRM, {"w_in": S.ah(22.0, 43.0), "w_out": 4.0, "t_in": 22.0, "t_out": 16.0})
    dehums = [m for m in models if m.function == "dehumidify"]
    vent = next(m for m in models if m.function == "ventilate")
    h = S.House(22.0, 43.0, 22.0, 4.0, 16.0, 85.0)
    _, d = S.drying_worth(BANDS, MONEY, PRM, h, dehums, vent, 1, 3, False)
    ratio = d["value_h"] / d["cost_h"]
    cost = MONEY
    # scale the ERV's fan power so value / extra cost lands at 0.9, between 1 − hyst and 1: running stays on,
    # a new start doesn't
    bump = {**BANDS, "outdoor_drying": {**BANDS["outdoor_drying"], "cost_hyst": 0.2}}
    mid = ratio / 0.9
    prm = M.params({"erv": {"fan_w": [x * mid for x in PRM["erv"]["fan_w"]]}})
    models2 = M.build(REG, prm, {"w_in": S.ah(22.0, 43.0), "w_out": 4.0, "t_in": 22.0, "t_out": 16.0})
    vent2 = next(m for m in models2 if m.function == "ventilate")
    w_start, d2 = S.drying_worth(bump, cost, prm, h, dehums, vent2, 1, 3, False)
    w_held, _ = S.drying_worth(bump, cost, prm, h, dehums, vent2, 1, 3, True)
    assert 0.8 < d2["value_h"] / d2["cost_h"] < 1.0 and not w_start and w_held
    off = {**BANDS, "outdoor_drying": {**BANDS["outdoor_drying"], "cost_check": False}}
    assert S.drying_worth(off, MONEY, PRM, h, dehums, vent2, 1, 3, False) == (True, None)


def test_aq_step_matches_the_erv_rule_bands():
    assert [S.aq_level(BANDS, a, 3) for a in (80, 75.5, 75, 62, 60, 40)] == [1, 1, 2, 2, 3, 3]


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
    assert B.validate({"bands": {"outdoor_drying": {"cost_hyst": 1.5}}}, cur)[0]
    assert B.validate({"bands": {"outdoor_drying": {"cost_check": "yes"}}}, cur)[0]
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
    assert last["choice"]["midea"]["label"] == "on"                       # the held answer's own label
    B.put(conn, store, {"enrol": {"midea": "rule"}})
    sh.run(conn, NOW + 2000, REG, h, {"midea": 0})
    plan = json.loads(conn.execute("SELECT plan FROM law_shadow ORDER BY ts DESC").fetchone()[0])
    assert "midea" not in plan                                # rule-enrolled: left out of the comparison
    v = SH.view(conn, store, NOW + 2000)
    assert v["agreement_24h"]["apr"]["ticks"] >= 1
    sh.run(conn, NOW + 2400, REG, h, {"midea": 1}, study_arm="MA")      # the experiment, not the rules
    v2 = SH.view(conn, store, NOW + 2400)
    assert v2["study_ticks_24h"] == 1 and v2["agreement_24h"]["apr"]["ticks"] == v["agreement_24h"]["apr"]["ticks"]


def test_shadow_hysteresis_runs_on_its_own_answers():
    # 11-20 03:27: RH dipped 41 → 40 for one tick; the law stopped drying, and on the rules' ERV-high it read
    # "already at top" and restarted at 41. On its own answer (low) it must wait for > 41.
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    B.put(conn, store, {"bands": {"outdoor_drying": {"cost_check": False}}})
    sh = SH.Shadow(store)
    h = lambda rh: _house(rh=rh, w_out=5.0)
    assert sh.run(conn, NOW, REG, h(43.0), {"erv": 3}).choice["erv"]["level"] == 3
    assert sh.run(conn, NOW + 1000, REG, h(40.0), {"erv": 3}).choice["erv"]["level"] == 1
    assert sh.run(conn, NOW + 2000, REG, h(41.0), {"erv": 3}).choice["erv"]["level"] == 1   # rule still high
    assert sh.run(conn, NOW + 3000, REG, h(42.0), {"erv": 3}).choice["erv"]["level"] == 3


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
    assert not ctrl._law.due(ctrl._law.last_run + 60)          # waits 2 min after start for telemetry
    ctrl._law.last_run = 0.0
    for sid in B.ROOMS:
        ctrl.readings[sid] = {"m": {"temperature_c": 21.5, "humidity_pct": 47.0}, "ts": NOW - 30}
    ctrl.tick(now=NOW)
    conn = sqlite3.connect(db)
    last = store.get_setting(conn, "law:last")
    assert last is not None and any("RH 47.0" in r for r in last["reasons"])
    assert iss.calls == []                                    # no policies: the law alone commands nothing
