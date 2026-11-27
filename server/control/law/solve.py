"""The unified law's tick — pure, no I/O (docs/design/unified-control-law.md §4).

Given the house's state, the user's bands, the cost basis and the actuator models, choose each actuator's option:

  1. Errors: how far each band is off. Temperature and RH are FIRM, air quality is SOFT (a weighted shortfall).
  2. Water need (g/h): above the RH band, remove the excess water over `horizon_h` (the house buffers water
     ~`buffer_x`× its air volume, so it's the buffered mass that has to go); once drying, keep going to the band's
     middle (the hysteresis the dehumidifier rules have today: on at the top, off part-way down); below the band,
     no drying at all.
  3. Try every combination of the dehumidifiers (off/on) and the ventilator's levels (both dehumidifiers together
     remove less than the sum: the house sees `midea.with_aprilaire` of the Midea's water then, Phase 4 §8). Rank by (firm violation, cost
     + the soft AQ shortfall × its weight, changes from now) and take the first. The actuators are few, so a full
     search beats a general optimiser, and every choice can be explained.
     Outdoor drying (Hugh 2026-11-17): while RH is in or above the band and outdoor air is drier by the dew-point gap
     (`outdoor_drying`, with the dry boost's hysteresis), the ventilator is wanted at its top level, like an AQ
     want; the house's water generation has been steady, so getting ahead of it is always temporary. Below the
     band the firm "no drying" term ramps it back down — to the AQ level, not below it (Hugh 2026-11-21).
     Cost check (Hugh 2026-11-20): in band, water removed early is worth at most what the cheapest dehumidifier
     would spend removing it later, so outdoor drying is wanted only while the extra water (top level vs the AQ
     level) is worth at least the extra fan + heat-loss cost; started at a ratio of 1, held to 1 − `cost_hyst`.
     Cold outdoor air raises the heat cost, so it ends in-band drying on its own. Above the band the water need
     already prices drying, and no check applies.
  4. Purifiers follow the rough AQ rule (Hugh 2026-11-12): one level up per `aq.step` below the floor.
  5. The thermostat isn't searched: the law asks for the temperature band itself (heat to `min`, cool at `max`).

Cost: `kwh` = electricity only. `money` = electricity at its rate, plus heat valued at the gas it saves while the
house is heating (a dehumidifier's heat replaces furnace heat) or the AC electricity it costs while cooling.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

THERM_KWH = 29.307
AC_COP = 3.0                        # assumed; single-stage AC, no metered figure


def ah(t_c: float, rh: float) -> float:
    es = 6.112 * math.exp(17.62 * t_c / (243.12 + t_c))
    return 216.7 * (rh / 100 * es) / (273.15 + t_c)


def ah_dp(dp_c: float, t_c: float) -> float:
    """Absolute humidity g/m³ of air at temperature t_c with dew point dp_c."""
    return 216.7 * 6.112 * math.exp(17.62 * dp_c / (243.12 + dp_c)) / (273.15 + t_c)


def dp_ah(w: float, t_c: float) -> float:
    """Dew point °C of air at temperature t_c holding w g/m³ (the inverse of ah_dp)."""
    g = math.log(w * (273.15 + t_c) / (216.7 * 6.112))
    return 243.12 * g / (17.62 - g)


def outdoor_drying(bands: dict, h: House, need: float | None, venting_top: bool) -> tuple[bool, float | None]:
    """(drying with outdoor air wanted, the dew-point gap indoor − outdoor). On at `on_dp_c`, held to `off_dp_c`
    while the ventilator already runs at its top level; it stops at the RH band's min and restarts above
    min + `restart_rh_pts` (Hugh 2026-11-19)."""
    od = bands.get("outdoor_drying") or {}
    if not od.get("enabled") or need is None or need < 0 or h.w_out is None or h.t_out_c is None or h.w_in is None:
        return False, None
    floor = float(bands["rh"]["min"]) + (0.0 if venting_top else float(od.get("restart_rh_pts", 0.0)))
    if h.rh <= floor:
        return False, None
    gap = dp_ah(h.w_in, h.t_rh_c) - dp_ah(h.w_out, h.t_out_c)
    return gap >= float(od["off_dp_c"] if venting_top else od["on_dp_c"]), gap


def drying_worth(bands: dict, cost: dict, prm: dict, h: House, dehums: list, vent, base: int, top: int,
                 venting_top: bool) -> tuple[bool, dict | None]:
    """In band: is running the ventilator at `top` instead of `base` (the AQ level) worth its cost? Water is valued
    at the cheapest dehumidifier's cost per g (its heat credited while heating, as `_cost` does). (worth, detail);
    detail None = no check (disabled, nothing to value the water by, or no step up)."""
    od = bands.get("outdoor_drying") or {}
    if not od.get("cost_check") or vent is None or top <= base:
        return True, None
    per_g = [_cost(cost, prm, h, o.elec_w, o.heat_w) / -o.water_gph
             for m in dehums for o in m.options if o.level and o.water_gph < 0]
    per_g = [x for x in per_g if x > 0]
    if not per_g:
        return True, None
    o_top, o_base = vent.options[top], vent.options[base]
    extra_g = o_base.water_gph - o_top.water_gph                 # g/h more removed at the top level
    extra_cost = (_cost(cost, prm, h, o_top.elec_w, o_top.heat_w)
                  - _cost(cost, prm, h, o_base.elec_w, o_base.heat_w))
    value = max(0.0, extra_g) * min(per_g)
    need_ratio = 1.0 - float(od.get("cost_hyst", 0.0)) if venting_top else 1.0
    worth = extra_cost <= 0 or value >= need_ratio * extra_cost
    return worth, {"extra_gph": round(extra_g), "value_h": round(value, 4), "cost_h": round(extra_cost, 4)}


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
    aq_by: dict = field(default_factory=dict)   # {purifier: its own room's AQ}, from its automation policy's sensor
                                                # (the card's picker; Hugh 2026-11-27). Missing = the house `aq`

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
    # drying starts AT the band's top (the rules' on_above is >=; 11-26 05:12 the Midea's rule started at 46.0 and
    # the law, waiting for > 46, didn't) and runs past the bottom to min − dry_past. It ran only to the middle until
    # 11-27: 6 runs of 16–27 min in 5 h where the rule (46 → 43) makes one long run; a compressor spends the first
    # minutes of each run cooling its coil, so the law now dries in long runs too (Hugh 2026-11-27)
    target = float(rh_b["min"]) - float(rh_b.get("dry_past") or 0.0)
    if not (drying_now and h.rh > target):              # a run in progress continues below the band
        if h.rh < rh_b["min"]:
            return -1.0
        if not drying_now and h.rh < rh_b["max"]:
            return 0.0
        if h.rh <= target:
            return 0.0
    # How MUCH to remove is still sized to the band's middle, as before; the run just doesn't end there. Below the
    # middle the need holds at the band-top start's size (half the band) and tapers to 0 at the stop point, so a
    # deeper stop never calls a second dehumidifier the start didn't (min(): above the top it still escalates)
    hp = prm["house"]
    vol = float(hp["air_m3"]) * float(hp["buffer_x"])
    mid = (float(rh_b["min"]) + float(rh_b["max"])) / 2
    over_mid = (h.w_in - ah(h.t_rh_c, mid)) * vol
    half_band = (ah(h.t_rh_c, float(rh_b["max"])) - ah(h.t_rh_c, mid)) * vol
    to_stop = (h.w_in - ah(h.t_rh_c, target)) * vol
    excess_g = max(over_mid, min(to_stop, half_band))
    return max(0.0, excess_g / float(bands["horizon_h"]) + float(hp.get("load_gph") or 0.0))


def aq_level(bands: dict, aq: float | None, top: int, held: int | None = None) -> int | None:
    """The rough AQ rule: level 1 above the floor, one more at it and per `step` below it, up to `top`. `held` = the
    level the device runs at now: it stays while the index is within `deadband` of the edge it would cross (the
    rules' own ±5 hold, automation.py; 11-21: AQ 74–80 around the 75 edge flipped the law 8 times, the rule twice)."""
    if aq is None:
        return None
    a = bands["aq"]

    def at(x: float) -> int:
        below = a["floor"] - x
        return min(top, 1 + (int(below // a["step"]) + 1 if below >= 0 else 0))   # the edge counts (ERV rule: <= 75)
    lvl, db = at(aq), float(a.get("deadband") or 0.0)
    if held and held != lvl and db > 0 and held in (at(aq - db), at(aq + db)):
        return min(top, held)
    return lvl


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
    thermo = any(m.function == "condition_temperature" for m in models)

    top = max([len(m.options) - 1 for m in vents] or [0])
    venting_top = bool(vents) and any((current.get(m.device_id) or 0) >= top for m in vents)
    dry_out, gap = outdoor_drying(bands, h, need, venting_top)
    vent_now = max([current.get(m.device_id) or 0 for m in vents] or [0])
    aq_need = aq_level(bands, h.aq, top, vent_now)
    aq_held = aq_need is not None and aq_need != aq_level(bands, h.aq, top)
    worth_d = None
    if dry_out and need == 0:                           # in band: only while the water is worth its cost
        worth, worth_d = drying_worth(bands, cost, prm, h, dehums, vents[0] if vents else None,
                                      min(aq_need or 0, top), top, venting_top)
        dry_out = worth
    vent_want = max(aq_need or 0, top if dry_out else 0)

    choices = [m.options for m in dehums] + [m.options for m in vents]
    ranked = []
    both_f = float(prm["midea"].get("with_aprilaire", 1.0))
    for combo in itertools.product(*choices) if choices else [()]:
        water = sum(o.water_gph for o in combo)
        on = {m.kind: o for m, o in zip(dehums, combo) if o.level}
        if "midea" in on and "aprilaire" in on:          # they dry the same air: the house sees less of the Midea
            water += on["midea"].water_gph * (both_f - 1)
        heat = sum(o.heat_w for o in combo)
        elec = sum(o.elec_w for o in combo)
        vent = max([o.vent_level for o in combo[len(dehums):]] or [0])
        firm = 0.0
        if need is not None and need > 0:
            firm += max(0.0, need + water)                  # removal short of the need (water < 0 = removal)
        elif need is not None and need < 0:
            # drying an already-dry house: the dehumidifiers, and ventilation BEYOND the AQ level. The AQ level's
            # own water doesn't count — air quality trumps (Hugh 2026-11-21: the law had the ERV off at 39 % with
            # AQ ~58 overnight; "air quality trumps heat savings")
            vent_extra = 0.0
            for m, o in zip(vents, combo[len(dehums):]):
                aq_o = m.options[min(aq_need or 0, len(m.options) - 1)]
                if o.level > aq_o.level:
                    vent_extra += o.water_gph - aq_o.water_gph
            dehum_water = water - sum(o.water_gph for o in combo[len(dehums):])
            firm += max(0.0, -(dehum_water + vent_extra))
        # Temperature is the thermostat's job when one is modelled (it holds the band). 11-27 06:00 PST the day goals
        # raised the band to 70 with the house at 69.7: this term ran the Midea in band as a heater, then — once RH
        # hit 42 — switched the ERV OFF (AQ 77, floor 80) to stop its heat loss. Air quality trumps heat savings
        # (Hugh 2026-11-21); the heat stays in the cost (credited against gas while heating). No thermostat: firm.
        if temp_hot and not thermo:
            firm += max(0.0, heat) / 100                    # heat into a house above its band (W → g/h-ish scale)
        if temp_cold and not thermo:
            firm += max(0.0, -heat) / 100
        aq_short = max(0, vent_want - vent) if vents else 0     # the AQ want, or outdoor drying's top level
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
                       + (f" (drying to {float(bands['rh']['min']) - float(bands['rh'].get('dry_past') or 0.0):g} %)" if drying_now else f" (at/above {bands['rh']['max']:g} %)"))
    elif need < 0:
        reasons.append(f"RH {h.rh:.1f} % below {bands['rh']['min']:g} %: no drying")
    else:
        reasons.append(f"RH {h.rh:.1f} % in band")
    if errs["temp_f"]:
        reasons.append(f"temperature {c2f(h.t_in_c):.1f} °F, {abs(errs['temp_f']):.1f} °F "
                       f"{'above' if temp_hot else 'below'} the band"
                       + (" (the thermostat's job)" if thermo else ""))
    if h.aq is not None and vents:
        reasons.append(f"AQ {h.aq:.0f} → ventilation level {aq_need} wanted"
                       + (f" (held within ±{float(bands['aq']['deadband']):g} of the band edge)" if aq_held else ""))
    basis_u = "kWh" if cost.get("basis") != "money" else "$"
    wd = (f"+{worth_d['extra_gph']} g/h worth {worth_d['value_h']:.3f} {basis_u}/h at the cheapest dehumidifier's "
          f"cost vs {worth_d['cost_h']:.3f} {basis_u}/h fan + heat loss") if worth_d else ""
    if dry_out:
        reasons.append(f"outdoor dew point {gap:.1f} °C below indoor → ventilation level {top} to dry"
                       + (f" ({wd})" if wd else ""))
    elif worth_d:
        reasons.append(f"outdoor dew point {gap:.1f} °C below indoor, in band: drying not worth it ({wd})")
    reasons.append("chose " + ", ".join(f"{d} {v['label']}" for d, v in choice.items())
                   + f": {best[2]['water_gph']:+.0f} g/h water, {best[2]['elec_w']} W, {best[2]['cost_h']:.3f} {basis}"
                   + (f", firm shortfall {best[2]['firm']}" if best[2]['firm'] else "")
                   + (f", ventilation short {best[2]['aq_short']} level(s)" if best[2]['aq_short'] else ""))

    # purifiers: the rough AQ rule; the thermostat: the band itself
    # each purifier by its own room's sensor (Hugh 2026-11-27: 11-26 overnight c_bed, room 68, ran speed 1 on the
    # ERV's mean of 4 rooms where its own rule says 2); the house `aq` only when its room has no fresh reading
    rooms = []
    for m in models:
        if m.function == "purify_air":
            aq_p = h.aq_by.get(m.device_id, h.aq)
            lvl = aq_level(bands, aq_p, 4, current.get(m.device_id))
            if lvl is not None:
                choice[m.device_id] = {"level": lvl, "label": f"speed {lvl}"}
                rooms.append(f"{m.device_id} {aq_p:.0f} → {lvl}" + ("" if m.device_id in h.aq_by else " (house AQ)"))
        elif m.function == "condition_temperature":
            choice[m.device_id] = {"level": None, "label": f"heat to {bands['temp_f']['min']:g} °F, "
                                                           f"cool above {bands['temp_f']['max']:g} °F"}
    if rooms:
        reasons.append("purifiers by their rooms' AQ: " + ", ".join(rooms))
    alts = [{"choice": {m.device_id: o.label for m, o in zip(dehums + vents, r[1])}, **r[2]} for r in ranked[1:4]]
    return Plan(choice, errs, need, best[2], reasons, alts)
