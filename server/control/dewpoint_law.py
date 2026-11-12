"""ERV dew-point law — pure, no I/O (Hugh, 2026-11-11). Replaces the one-way dry boost and wet trim with one law.

Every `step_c` of dew-point gap between indoor and outdoor air moves the ERV one level from what indoor air quality
asks for — up when outdoor air is drier (fresh air dries the house), down when it is wetter (fresh air is moisture
the dehumidifiers must remove) — topping out at the ERV's top level and bottoming out at off. Two opposing forces
fight it, each cancelling only its own direction ("those need to fight", Hugh):

    dry  = steps(dp_in − dp_out)        cold = steps(cold_start_c − t_out, cold_step_c)   boost = max(0, dry − cold)
    wet  = steps(dp_out − dp_in)        aq   = steps(aq_start − air quality, aq_step)     trim  = max(0, wet − aq)
    level = clamp(air-quality level + boost − trim, 0 .. top)          steps(x, s) = floor(x / s) for x > 0, else 0

  - Cold outdoor air pushes against the boost: it is cold air into the house and frost load on the ERV core. The
    default start (10 °C / 50 °F) and step follow Broan's own AI Series outdoor-temperature ventilation schedule
    (full ventilation above 50 °F, 40 min/h from 50 to 19 °F, 20 min/h below: docs/design/hvac-shade-device-
    integration.md §1.10). Cold can cancel a boost, never pull the ERV below what air quality asks for.
  - Poor indoor air pushes against the trim (the air-quality index: lower is worse). It can cancel a trim, never
    raise the ERV above what air quality asks for (the air-quality bands already do that).
  - Hard guards (not forces): no boost while any room is under min_room_c (the house itself is cold); optional
    RH floors — no boost below `boost_rh_floor` (don't over-dry), no trim below `trim_rh_floor` (the house is dry
    enough that fresh air costs nothing to dry). `trim_rh_floor` is a fixed RH, null (none), or "dehumidifiers":
    trim only while RH is above at least one running dehumidifier's shut-off point, on that unit's own sensor
    (the controller works that out; `dehum_ok` here).

Doubt pushes nowhere. Outdoor dew point: an unexplained sensor/Open-Meteo disagreement takes the WETTER reading for
the boost and the DRIER for the trim (dry_boost.outdoor_dewpoint), so a disagreement can only shrink both. Outdoor
temperature: cross-checked the same way; a disagreement beyond agree_t_c takes the COLDER (more cold force, less
boost). Any input stale or missing → that term is 0; the outdoor or every indoor reading stale → no change at all.

Hysteresis: a bigger change in the same direction is taken at its edge; a smaller or reversing one only once the
inputs, nudged by hysteresis_c / hysteresis_t_c / hysteresis_aq back toward the current value, no longer allow it; changes wait min_hold_min, except a release to 0 when the inputs go away and the
cold-room guard, which act at once. State (net, since) lives in the controller, in memory.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from server.control import dry_boost

DEFAULTS = {
    "enabled": False,
    "step_c": None,             # °C of dew-point gap per level; None = disabled
    "hysteresis_c": 0.5,
    "min_hold_min": 15,
    # cold outdoor air vs the boost (Broan's schedule: full ventilation above 50 °F, 2/3 to 19 °F, 1/3 below)
    "cold_start_c": 10.0,
    "cold_step_c": 8.5,         # 10 °C → 1 notch off the boost at 1.5 °C (35 °F), 2 at −7 °C (19 °F)
    "hysteresis_t_c": 0.5,
    "agree_t_c": 2.0,           # sensor vs Open-Meteo outdoor temperature (the meter agrees within ~1 °C, 2026-11-08)
    # poor indoor air vs the trim (the air-quality index, 0–100, lower = worse)
    "aq_start": 75.0,           # the ERV's own level-2 band edge
    "aq_step": 10.0,
    "hysteresis_aq": 2.0,
    # hard guards
    "min_room_c": 18.0,
    "boost_rh_floor": None,     # house RH below this: no boost (None = no floor)
    "trim_rh_floor": None,      # house RH below this: no trim; None = no floor; "dehumidifiers" = track them
    # sensors + outdoor dew-point cross-check: unset keys fall back to the policy's dry_boost block
    "outdoor_sensor": None,
    "indoor_sensors": [],
}
_SHARED = dry_boost.DEFAULTS.keys() & {"outdoor_sensor", "indoor_sensors", "reference", "stale_min", "agree_c",
                                       "wet_rh", "reference_stale_min"}


def config(raw: dict | None, dry_raw: dict | None = None) -> dict:
    raw, dry = raw or {}, dry_boost.config(dry_raw)
    cfg = {**DEFAULTS, **{k: dry[k] for k in _SHARED}, **{k: v for k, v in raw.items() if v not in (None, [])}}
    for k in ("step_c", "boost_rh_floor", "trim_rh_floor"):     # an explicit null is a choice, not "unset"
        if k in raw:
            cfg[k] = raw[k]
    return cfg


def enabled(cfg: dict) -> bool:
    return (cfg.get("enabled") is True and bool(cfg.get("step_c")) and bool(cfg.get("outdoor_sensor"))
            and bool(cfg.get("indoor_sensors")))


def steps(x: float, step: float) -> int:
    return max(0, math.floor(x / step)) if x > 0 else 0


def outdoor_temperature(t_sensor: float, reference: tuple | None, cfg: dict, now: float) -> tuple[float, str]:
    """(outdoor temperature for the cold force, source). Open-Meteo (t, rh, ts) cross-checks the meter, which
    reads warm in sun; a disagreement beyond agree_t_c takes the colder. Stale/missing reference → the meter."""
    if not cfg.get("reference") or reference is None or now - reference[2] > float(cfg["reference_stale_min"]) * 60:
        return t_sensor, "sensor"
    t_r = float(reference[0])
    if abs(t_sensor - t_r) <= float(cfg["agree_t_c"]):
        return t_sensor, "sensor"
    return min(t_sensor, t_r), f"the colder (sensor {t_sensor:.1f}, Open-Meteo {t_r:.1f} °C disagree)"


@dataclass(frozen=True)
class Inputs:
    gap_boost: float            # dp_in − dp_out, outdoor resolved WETTER on doubt
    gap_trim: float             # dp_in − dp_out, outdoor resolved DRIER on doubt
    t_out: float | None
    aq: float | None            # the policy's air-quality reading (None = stale/missing)
    rh_in: float
    t_room_min: float
    dehum_ok: bool | None = None    # "dehumidifiers" floor: True = some dehumidifier would run; None = not used


@dataclass(frozen=True)
class Terms:
    dry: int
    wet: int
    cold: int
    aq: int
    boost: int
    trim: int
    blocked: str = ""           # which hard guard zeroed a term, if any

    @property
    def net(self) -> int:
        return self.boost - self.trim


def terms(cfg: dict, x: Inputs, gap_nudge: float = 0.0, t_nudge: float = 0.0, aq_nudge: float = 0.0) -> Terms:
    """The four forces and the net change for inputs `x`; the nudges shift the inputs for the hysteresis test
    (positive = toward a higher level: a bigger gap, warmer outdoor air, worse indoor air)."""
    step = float(cfg["step_c"])
    dry = steps(x.gap_boost + gap_nudge, step)
    wet = steps(-(x.gap_trim + gap_nudge), step)
    cold = (steps(float(cfg["cold_start_c"]) - (x.t_out + t_nudge), float(cfg["cold_step_c"]))
            if x.t_out is not None and cfg.get("cold_start_c") is not None and cfg.get("cold_step_c") else 0)
    aqf = (steps(float(cfg["aq_start"]) - (x.aq - aq_nudge), float(cfg["aq_step"]))
           if x.aq is not None and cfg.get("aq_start") is not None and cfg.get("aq_step") else 0)
    boost, trim, blocked = max(0, dry - cold), max(0, wet - aqf), []
    if boost and x.t_room_min < float(cfg["min_room_c"]):
        boost = 0
        blocked.append(f"no boost: a room below {float(cfg['min_room_c']):.0f} °C")
    if boost and cfg.get("boost_rh_floor") is not None and x.rh_in < float(cfg["boost_rh_floor"]):
        boost = 0
        blocked.append(f"no boost: RH below {float(cfg['boost_rh_floor']):.0f} %")
    if trim and x.aq is None:
        trim = 0
        blocked.append("no trim: air quality unknown")
    floor = cfg.get("trim_rh_floor")
    if trim and floor == "dehumidifiers" and x.dehum_ok is not True:
        trim = 0
        blocked.append("no trim: RH below every running dehumidifier's shut-off" if x.dehum_ok is False
                       else "no trim: no dehumidifier automation running")
    elif trim and isinstance(floor, (int, float)) and x.rh_in < float(floor):
        trim = 0
        blocked.append(f"no trim: RH below {float(floor):.0f} %")
    return Terms(dry, wet, cold, aqf, boost, trim, "; ".join(blocked))


def decide(cfg: dict, x: Inputs | None, cur: int, since: float | None, now: float,
           why: str = "") -> tuple[int, str, Terms | None]:
    """(net level change wanted, reason, the terms). `cur`/`since` = the current change and when it began."""
    if x is None:
        return 0, f"dew-point law off: {why}", None
    t = terms(cfg, x)
    want = t.net
    away = abs(want) > abs(cur) and want * cur >= 0              # a bigger change, same direction: at the edge
    if want != cur and not away:                                  # smaller or reversing: only past the hysteresis
        hc, ht, ha = float(cfg["hysteresis_c"]), float(cfg["hysteresis_t_c"]), float(cfg["hysteresis_aq"])
        if (cur > want and terms(cfg, x, hc, ht, ha).net >= cur) or \
                (cur < want and terms(cfg, x, -hc, -ht, -ha).net <= cur):
            want = cur
    held = (now - since) / 60 if since is not None else float("inf")
    cold_room = cur > 0 and "room below" in t.blocked
    if want != cur and held < float(cfg["min_hold_min"]) and not cold_room:
        want = cur
        hold = f" (held {float(cfg['min_hold_min']) - held:.0f}m)"
    else:
        hold = ""
    s = (f"dew-point gap {x.gap_boost:+.1f} °C" + (f"/{x.gap_trim:+.1f}" if x.gap_trim != x.gap_boost else "")
         + f": dry {t.dry} wet {t.wet}, cold {t.cold}"
         + ("" if x.t_out is None else f" ({x.t_out:.1f} °C out)")
         + f", air {t.aq}" + ("" if x.aq is None else f" (AQ {x.aq:.0f})")
         + (f"; {t.blocked}" if t.blocked else ""))
    return want, f"dew-point law {want:+d}{hold}: {s}", t
