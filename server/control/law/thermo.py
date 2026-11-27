"""Thermostat setpoints that hold the ROOMS at the band, not the thermostat's own spot (Hugh 2026-11-27).

The Venstar regulates on its hall sensor; the band is the mean of the room meters (`temp_sensors`). 11-27 07:30 PST
the hall read 72.0 °F with the rooms at 69.5: heat set to 70, it sat idle and the rooms stayed under the band. So the
setpoints carry the hall-vs-rooms offset — smoothed (`tau_min`, so a furnace cycle's warm-up doesn't steer it),
capped at ±`max_f` — and are whole °F the thermostat takes: heat rounded UP (the rooms reach the band's bottom, Hugh:
"round up for the thermostat"), cool rounded DOWN (they stay under its top). A setpoint already written holds while
the raw target stays within `HYST_F` of its rounding range, so the offset drifting across a degree edge doesn't flip
the thermostat every pass. Cool stays `MIN_GAP_F` above heat (the thermostat's setpoint delta).

Pure; the controller (_apply_law_thermostats) feeds it readings and writes the result.
"""
from __future__ import annotations

import math

KEY_PREFIX = "law:thermo:"     # control.db house_setting: what the law sets each thermostat to, and why (the PWA)
OFFSET_DEFAULT = {"enabled": True, "max_f": 4.0, "tau_min": 30.0}
HYST_F = 0.25
MIN_GAP_F = 2.0


def smooth(prev: float | None, prev_ts: float | None, sample: float, now: float, tau_min: float) -> float:
    """Exponential smoothing with time constant `tau_min` (irregular sample spacing)."""
    if prev is None or prev_ts is None or tau_min <= 0:
        return sample
    a = 1.0 - math.exp(-max(0.0, now - prev_ts) / (tau_min * 60.0))
    return prev + a * (sample - prev)


def _heat(raw: float, have: float | None) -> float:
    if have is not None and float(have).is_integer() and have - 1 - HYST_F < raw <= have + HYST_F:
        return float(have)                                  # ceil's range for `have` is (have-1, have]
    return float(math.ceil(raw - 1e-6))


def _cool(raw: float, have: float | None) -> float:
    if have is not None and float(have).is_integer() and have - HYST_F <= raw < have + 1 + HYST_F:
        return float(have)                                  # floor's range for `have` is [have, have+1)
    return float(math.floor(raw + 1e-6))


def setpoints(band_min: float, band_max: float, offset_f: float | None, cfg: dict,
              have_heat: float | None = None, have_cool: float | None = None) -> tuple[dict, float]:
    """({"heat": °F, "cool": °F}, the offset applied). offset_f = thermostat reading − rooms' mean (°F; + = the
    thermostat's spot is warmer); None or disabled = no offset (the band itself, still whole °F)."""
    c = {**OFFSET_DEFAULT, **(cfg or {})}
    off = 0.0
    if c.get("enabled") and offset_f is not None:
        lim = float(c["max_f"])
        off = max(-lim, min(lim, float(offset_f)))
    heat = _heat(band_min + off, have_heat)
    cool = _cool(band_max + off, have_cool)
    if cool - heat < MIN_GAP_F:
        cool = heat + MIN_GAP_F
    return {"heat": heat, "cool": cool}, round(off, 2)
