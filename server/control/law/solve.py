"""The unified law's tick — pure, no I/O (docs/design/unified-control-law.md §4).

Given the house's state, the user's bands, the cost basis and the actuator models, choose each actuator's option:

  1. Errors: how far each band is off. Temperature and RH are FIRM, air quality is SOFT (a weighted shortfall).
  2. Water need (g/h): above the RH band, remove the excess water over `horizon_h` (the house buffers water
     ~`buffer_x`× its air volume, so it's the buffered mass that has to go); once drying, keep going to the band's
     middle (the hysteresis the dehumidifier rules have today: on at the top, off part-way down); below the band,
     no drying at all.
  3. Try every combination of the dehumidifiers (off/on) and the ventilator's levels. Rank by (firm violation, cost
     + the soft AQ shortfall × its weight, changes from now) and take the first. The actuators are few, so a full
     search beats a general optimiser, and every choice can be explained.
  4. Purifiers follow the rough AQ rule (Hugh 2026-11-12): one level up per `aq.step` below the floor.
  5. The thermostat isn't searched: the law asks for the temperature band itself (heat to `min`, cool at `max`).

Cost: `kwh` = electricity only. `money` = electricity at its rate, plus heat valued at the gas it saves while the
house is heating (a dehumidifier's heat replaces furnace heat) or the AC electricity it costs while cooling.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass

THERM_KWH = 29.307
AC_COP = 3.0                        # assumed; single-stage AC, no metered figure


def ah(t_c: float, rh: float) -> float:
    es = 6.112 * math.exp(17.62 * t_c / (243.12 + t_c))
    return 216.7 * (rh / 100 * es) / (273.15 + t_c)


def ah_dp(dp_c: float, t_c: float) -> float:
    """Absolute humidity g/m³ of air at temperature t_c with dew point dp_c."""
    return 216.7 * 6.112 * math.exp(17.62 * dp_c / (243.12 + dp_c)) / (273.15 + t_c)


def f2c(f: float) -> float:
    return (f - 32) / 1.8


def c2f(c: float) -> float:
    return c * 1.8 + 32


@dataclass(frozen=True)
class House:
    t_in_c: float | None            # house mean temperature
    rh: float | None                # RH on the band's sensor
    t_rh_c: float | None            # temperature at that sensor
    w_out: float | None             # outdoor absolute humidity g/m³ (cross-checked)
    t_out_c: float | None
    aq: float | None                # air-quality index, 0–100 (lower = worse)
    heating: bool = False           # the house is in heating (furnace recently on / below the temperature band)
    cooling: bool = False

    @property
    def w_in(self) -> float | None:
        return ah(self.t_rh_c, self.rh) if self.t_rh_c is not None and self.rh is not None else None


@dataclass(frozen=True)
class Plan:
    choice: dict                    # device_id -> {"level", "label"}
    errors: dict
    need_gph: float | None
    totals: dict                    # water_gph, heat_w, elec_w, cost_h, aq_short
    reasons: list
    alternatives: list              # the next-best combinations, for the explanation


def errors(bands: dict, h: House) -> dict:
    t, rh, aq = bands["temp_f"], bands["rh"], bands["aq"]
    e = {"temp_f": None, "rh": None, "aq": None}
    if h.t_in_c is not None:
        tf = c2f(h.t_in_c)
        e["temp_f"] = round(tf - t["max"], 2) if tf > t["max"] else round(tf - t["min"], 2) if tf < t["min"] else 0.0
    if h.rh is not None:
        e["rh"] = round(h.rh - rh["max"], 2) if h.rh > rh["max"] else round(h.rh - rh["min"], 2) if h.rh < rh["min"] else 0.0
    if h.aq is not None:
        e["aq"] = round(min(0.0, h.aq - aq["floor"]), 1)
    return e


def water_need(bands: dict, prm: dict, h: House, drying_now: bool) -> float | None:
    """g/h of water to remove (> 0), 0 = hold, < 0 = the house is too dry (no drying wanted). None = unknown."""
    if h.rh is None or h.t_rh_c is None:
        return None
    rh_b = bands["rh"]
    if h.rh < rh_b["min"]:
        return -1.0
    target = rh_b["max"] if not drying_now else (rh_b["min"] + rh_b["max"]) / 2
    if h.rh <= target:
        return 0.0
    hp = prm["house"]
    excess_g = (h.w_in - ah(h.t_rh_c, target)) * float(hp["air_m3"]) * float(hp["buffer_x"])
    return max(0.0, excess_g / float(bands["horizon_h"]) + float(hp.get("load_gph") or 0.0))


def aq_level(bands: dict, aq: float | None, top: int) -> int | None:
    """The rough AQ rule: level 1 at or above the floor, one more per `step` below it, up to `top`."""
    if aq is None:
        return None
    a = bands["aq"]
    below = a["floor"] - aq
    return min(top, 1 + (int(below // a["step"]) + 1 if below > 0 else 0))


def _cost(cost: dict, prm: dict, h: House, elec_w: float, heat_w: float) -> float:
    if cost.get("basis") != "money":
        return elec_w / 1000
    pe = float(cost["electric_per_kwh"])
    c = elec_w / 1000 * pe
    if h.heating:
        c -= heat_w / 1000 / float(prm["furnace"]["efficiency"]) * float(cost["gas_per_therm"]) / THERM_KWH
    elif h.cooling:
        c += heat_w / 1000 / AC_COP * pe
    return c


def solve(models: list, bands: dict, cost: dict, prm: dict, h: House, current: dict) -> Plan:
    """`current` = {device_id: level now} (0 = off). Returns the plan for every modelled actuator."""
    errs = errors(bands, h)
    dehums = [m for m in models if m.function == "dehumidify"]
    vents = [m for m in models if m.function == "ventilate"]
    drying_now = any((current.get(m.device_id) or 0) > 0 for m in dehums)
    need = water_need(bands, prm, h, drying_now)
    reasons = []
    temp_hot = errs["temp_f"] is not None and errs["temp_f"] > 0
    temp_cold = errs["temp_f"] is not None and errs["temp_f"] < 0

    choices = [m.options for m in dehums] + [m.options for m in vents]
    ranked = []
    for combo in itertools.product(*choices) if choices else [()]:
        water = sum(o.water_gph for o in combo)
        heat = sum(o.heat_w for o in combo)
        elec = sum(o.elec_w for o in combo)
        vent = max([o.vent_level for o in combo[len(dehums):]] or [0])
        firm = 0.0
        if need is not None and need > 0:
            firm += max(0.0, need + water)                  # removal short of the need (water < 0 = removal)
        elif need is not None and need < 0:
            firm += max(0.0, -water)                        # anything drying an already-dry house
        if temp_hot:
            firm += max(0.0, heat) / 100                    # heat into a house above its band (W → g/h-ish scale)
        if temp_cold:
            firm += max(0.0, -heat) / 100
        aq_need = aq_level(bands, h.aq, max([len(m.options) - 1 for m in vents] or [0]))
        aq_short = max(0, (aq_need or 0) - vent) if vents else 0
        c = _cost(cost, prm, h, elec, heat)
        changes = sum(1 for m, o in zip(dehums + vents, combo) if current.get(m.device_id) != o.level)
        ranked.append(((round(firm, 1), round(c + float(bands["aq"]["weight"]) * aq_short, 4), changes),
                       combo, {"water_gph": round(water, 1), "heat_w": round(heat), "elec_w": round(elec),
                               "cost_h": round(c, 4), "aq_short": aq_short, "firm": round(firm, 1)}))
    ranked.sort(key=lambda r: r[0])
    best = ranked[0]
    choice = {m.device_id: {"level": o.level, "label": o.label} for m, o in zip(dehums + vents, best[1])}

    # the explanation: what's off, what was needed, why this combination won
    basis = "kWh/h" if cost.get("basis") != "money" else "$/h"
    if need is None:
        reasons.append("RH unknown: no water term")
    elif need > 0:
        reasons.append(f"RH {h.rh:.1f} % → remove {need:.0f} g/h"
                       + (" (drying to the band's middle)" if drying_now else f" (above {bands['rh']['max']:g} %)"))
    elif need < 0:
        reasons.append(f"RH {h.rh:.1f} % below {bands['rh']['min']:g} %: no drying")
    else:
        reasons.append(f"RH {h.rh:.1f} % in band")
    if errs["temp_f"]:
        reasons.append(f"temperature {c2f(h.t_in_c):.1f} °F, {abs(errs['temp_f']):.1f} °F "
                       f"{'above' if temp_hot else 'below'} the band")
    if h.aq is not None and vents:
        reasons.append(f"AQ {h.aq:.0f} → ventilation level {aq_level(bands, h.aq, len(vents[0].options) - 1)} wanted")
    reasons.append("chose " + ", ".join(f"{d} {v['label']}" for d, v in choice.items())
                   + f": {best[2]['water_gph']:+.0f} g/h water, {best[2]['elec_w']} W, {best[2]['cost_h']:.3f} {basis}"
                   + (f", firm shortfall {best[2]['firm']}" if best[2]['firm'] else "")
                   + (f", AQ short {best[2]['aq_short']} level(s)" if best[2]['aq_short'] else ""))

    # purifiers: the rough AQ rule; the thermostat: the band itself
    for m in models:
        if m.function == "purify_air":
            lvl = aq_level(bands, h.aq, 4)
            if lvl is not None:
                choice[m.device_id] = {"level": lvl, "label": f"speed {lvl}"}
        elif m.function == "condition_temperature":
            choice[m.device_id] = {"level": None, "label": f"heat to {bands['temp_f']['min']:g} °F, "
                                                           f"cool above {bands['temp_f']['max']:g} °F"}
    alts = [{"choice": {m.device_id: o.label for m, o in zip(dehums + vents, r[1])}, **r[2]} for r in ranked[1:4]]
    return Plan(choice, errs, need, best[2], reasons, alts)
