"""Dew-point wet trim for a level-mode ventilator (the ERV) — pure, no I/O (Hugh, 2026-11-10).

The mirror of the dry boost (server/control/dry_boost.py). When outdoor air is wetter than the house, every bit of
fresh air brings moisture the dehumidifiers then have to remove. The trim lowers the ERV one level for every
`step_c` of outdoor-over-indoor dew point, all the way down to 0 (off):

    Δ = outdoor dew point − indoor dew point        drops = floor(Δ / step_c)  (0 when Δ ≤ 0)
    level = max(0, air-quality level − drops)       level 0 = the ERV's off mode

`step_c` is the user's choice; `enabled` false (the PWA switch) or step_c null = disabled. Hysteresis: a drop is taken at k·step_c and given back only below
k·step_c − hysteresis_c, and the drop count holds at least min_hold_min between changes (except a release, below).

No trim — released at once — when:
  - the policy's own sensor value is at or below `release_below` (the ERV's sensor is the air-quality index, where
    lower is worse: bad air gets its fresh air back whatever the weather). null = no air-quality release;
  - indoor RH is below `rh_min` (the house is dry enough; fresh air costs nothing to dry);
  - the outdoor or every indoor reading is stale/missing.

Indoor/outdoor dew points reuse dry_boost.conditions, defaulting to the dry boost's sensors and Open-Meteo cross-
check, with one difference: an unexplained sensor/Open-Meteo disagreement resolves to the DRIER of the two (no trim
on doubt), where the boost takes the wetter (no boost on doubt).

Precedence: applies only to a running rule/default resolution — safety, operator overrides, scenes and schedules
beat it, and the dehumidifier's ventilation floor (applied after) still raises it. State (drops, since, base level)
lives in the controller, in memory: a restart drops it and the trim re-arms on the next tick.
"""
from __future__ import annotations

import math

from server.control import dry_boost

DEFAULTS = {
    "enabled": True,            # the PWA's switch: False keeps the settings but trims nothing
    "step_c": None,             # °C of outdoor-over-indoor dew point per one-level drop; None = disabled
    "hysteresis_c": 0.5,
    "min_hold_min": 15,
    "rh_min": 45.0,             # indoor RH below this: no trim
    "release_below": 60.0,      # policy sensor (air-quality index) at or below this: no trim; None = never
    # sensors + outdoor cross-check: unset keys fall back to the policy's dry_boost block, then dry_boost's defaults
    "outdoor_sensor": None,
    "indoor_sensors": [],
}
_SHARED = ("outdoor_sensor", "indoor_sensors", "reference", "stale_min", "agree_c", "wet_rh", "reference_stale_min")


def config(raw: dict | None, dry_raw: dict | None = None) -> dict:
    """The effective wet-trim config: its own keys, then the dry boost's sensors / cross-check settings."""
    raw, dry = raw or {}, dry_boost.config(dry_raw)
    cfg = {**DEFAULTS, **{k: dry[k] for k in _SHARED}, **{k: v for k, v in raw.items() if v not in (None, [])}}
    if "step_c" in raw:
        cfg["step_c"] = raw["step_c"]
    if "release_below" in raw:
        cfg["release_below"] = raw["release_below"]
    return cfg


def enabled(cfg: dict) -> bool:
    return (cfg.get("enabled") is not False and bool(cfg.get("step_c")) and bool(cfg.get("outdoor_sensor"))
            and bool(cfg.get("indoor_sensors")))


def drops_for(delta: float, step: float, cur: int, hyst: float) -> int:
    """Level drops for an outdoor-over-indoor dew-point gap `delta`, with hysteresis against the current count."""
    n = max(0, math.floor(delta / step)) if delta > 0 else 0
    if n < cur and delta >= cur * step - hyst:
        return cur
    return n


def decide(cfg: dict, cond, aq: float | None, cur: int, since: float | None, now: float,
           why: str = "") -> tuple[int, str]:
    """(level drops wanted, reason). `cond` = dry_boost.Conditions or None; `aq` = the policy sensor's value;
    `cur`/`since` = the current drop count and when it began (None = unknown)."""
    if cond is None:
        return 0, f"wet trim off: {why}"
    gap = -cond.delta                                   # Conditions.delta is indoor − outdoor
    s = (f"dp out {cond.dp_out:.1f} / in {cond.dp_in:.1f} °C (+{gap:.1f}), RH {cond.rh_in:.0f} %"
         + ("" if cond.out_src == "sensor" else f"; outdoor from {cond.out_src}"))
    rel = cfg.get("release_below")
    if rel is not None and aq is not None and aq <= float(rel):
        return 0, f"wet trim off: air quality {aq:.0f} ≤ {float(rel):.0f}; {s}"
    if cond.rh_in < float(cfg["rh_min"]):
        return 0, f"wet trim off: RH below {float(cfg['rh_min']):.0f} %; {s}"
    want = drops_for(gap, float(cfg["step_c"]), cur, float(cfg["hysteresis_c"]))
    held = (now - since) / 60 if since is not None else float("inf")
    if want != cur and held < float(cfg["min_hold_min"]):
        return cur, f"wet trim held at −{cur} (min-hold {float(cfg['min_hold_min']) - held:.0f}m left); {s}"
    if want == 0:
        return 0, f"wet trim idle: {s}"
    return want, f"wet trim −{want} (step {float(cfg['step_c']):g} °C): {s}"
