"""Dew-point dry boost for a level-mode ventilator (the ERV) — pure, no I/O (Hugh, 2026-11-04).

When outdoor air is drier than the house, more ventilation dries the house for the cost of an ERV fan and some
gas heat, instead of Aprilaire electricity (electricity >> gas here). The boost raises the ERV to `level` while

    outdoor dew point <= indoor dew point - on_delta_c   and   indoor RH >= rh_on   and   every room >= min_room_c

and releases it when the gap closes below off_delta_c (after min_on_min), or at once when indoor RH falls to
rh_floor (Hugh's floor: the ERV no longer boosts at 40 %) or a room drops below min_room_c. Restarts wait
min_off_min. Indoor = the mean over `indoor_sensors` (rooms, each needing temperature + RH); outdoor = one
sensor. A stale or missing outdoor reading means no boost.

Outdoor cross-check (Hugh, 2026-11-08). The outdoor meter sits on the garage's north wall, open to rain and dew, and
reads RH high when wet (dew point +1.4 °C median against Open-Meteo over 30 days, +2.2 °C at >= 95 % RH;
temperature agrees). That made the boost miss dry air. With `reference: "openmeteo"` the outdoor dew point is
cross-checked against ha-2's own Open-Meteo pull (weather.db, via the air-gap relay):
  - they agree within agree_c                                  -> the sensor
  - the sensor reads wetter by more, AND its RH >= wet_rh       -> Open-Meteo (the sensor's known wet bias)
  - any other disagreement (incl. Open-Meteo wetter)            -> the WETTER of the two (no boost on doubt)
  - Open-Meteo stale (> reference_stale_min) or missing         -> the sensor alone, as before
So ha-2 still runs on the sensor alone if the relay is down.

Precedence (Hugh, 2026-11-04, "boost wins, AQ otherwise"): an active boost overrides the air-quality level;
otherwise the air-quality rule runs unchanged. Safety, operator overrides, scenes and schedules still beat it —
the controller only applies this to a running rule/default resolution. The boost only ever RAISES the level.

State (active, since) lives in the controller, in memory: a restart drops it, and the boost re-arms on the next
tick that meets the start conditions.
"""
from __future__ import annotations

from dataclasses import dataclass

from server.util.psychro import dewpoint_c

DEFAULTS = {
    "outdoor_sensor": None,     # None = disabled
    "indoor_sensors": [],
    "on_delta_c": 2.0,
    "off_delta_c": 1.0,
    "rh_on": 41.0,
    "rh_floor": 40.0,
    "min_room_c": 18.0,
    "level": 3,                 # ERV: high (~124 cfm; turbo moves no more air here)
    "min_on_min": 30,
    "min_off_min": 30,
    "stale_min": 15,            # the outdoor meter reports every ~100 s (max gap ~6 min, 2026-11-04)
    "reference": None,          # "openmeteo" = cross-check the outdoor sensor (above); None = sensor only
    "agree_c": 1.5,
    "wet_rh": 80.0,             # the sensor's wet bias: +11 % RH at 80-95 %, +14 % at >= 95 % (2026-11-08)
    "reference_stale_min": 45,  # the relay pulls every ~15 min, jittered
}


def config(raw: dict | None) -> dict:
    return {**DEFAULTS, **(raw or {})}


@dataclass(frozen=True)
class Conditions:
    dp_in: float
    dp_out: float
    rh_in: float
    t_min: float
    n_in: int
    out_src: str = "sensor"

    @property
    def delta(self) -> float:
        return self.dp_in - self.dp_out


def outdoor_dewpoint(sensor: tuple, reference: tuple | None, cfg: dict, now: float) -> tuple[float, str]:
    """(outdoor dew point, which source) from the sensor's (t, rh) and the reference's (t, rh, ts) or None."""
    dp_s = dewpoint_c(*sensor)
    if not cfg.get("reference") or reference is None or now - reference[2] > float(cfg["reference_stale_min"]) * 60:
        return dp_s, "sensor"
    dp_r = dewpoint_c(reference[0], reference[1])
    if dp_r is None:
        return dp_s, "sensor"
    if abs(dp_s - dp_r) <= float(cfg["agree_c"]):
        return dp_s, "sensor (Open-Meteo agrees)"
    if dp_s > dp_r and sensor[1] >= float(cfg["wet_rh"]):
        return dp_r, f"Open-Meteo (sensor {dp_s:.1f} °C at RH {sensor[1]:.0f} %: wet bias)"
    return max(dp_s, dp_r), f"the wetter source (sensor {dp_s:.1f}, Open-Meteo {dp_r:.1f} °C disagree)"


def conditions(readings: dict, cfg: dict, now: float, reference: tuple | None = None) -> tuple[Conditions | None, str]:
    """Indoor/outdoor conditions from the controller's reading cache ({sid: {"m": {...}, "ts": epoch}}).
    `reference` = Open-Meteo's latest (t, rh, ts), used only with cfg reference set.
    Returns (None, why) when the outdoor reading or every indoor one is missing/stale."""
    stale = float(cfg["stale_min"]) * 60

    def th(sid):
        rec = readings.get(sid)
        if rec is None or now - rec["ts"] > stale:
            return None
        t, h = rec["m"].get("temperature_c"), rec["m"].get("humidity_pct")
        return (t, h) if t is not None and h is not None else None

    out = th(cfg["outdoor_sensor"])
    if out is None or dewpoint_c(*out) is None:
        return None, "outdoor reading stale/missing"
    rooms = [x for x in (th(s) for s in cfg["indoor_sensors"]) if x is not None and dewpoint_c(*x) is not None]
    if not rooms:
        return None, "indoor readings stale/missing"
    dp_out, src = outdoor_dewpoint(out, reference, cfg, now)
    return Conditions(dp_in=sum(dewpoint_c(*r) for r in rooms) / len(rooms), dp_out=dp_out,
                      rh_in=sum(h for _, h in rooms) / len(rooms), t_min=min(t for t, _ in rooms),
                      n_in=len(rooms), out_src=src), ""


def decide(cfg: dict, cond: Conditions | None, active: bool, since: float | None, now: float,
           why: str = "") -> tuple[bool, str]:
    """(boost wanted, reason). `since` = when the current state (active or not) began; None = unknown."""
    if cond is None:
        return False, f"dry boost off: {why}"
    held = (now - since) / 60 if since is not None else float("inf")
    s = (f"dp in {cond.dp_in:.1f} / out {cond.dp_out:.1f} °C (Δ {cond.delta:.1f}), RH {cond.rh_in:.0f} %, "
         f"coldest room {cond.t_min:.1f} °C" + ("" if cond.out_src == "sensor" else f"; outdoor from {cond.out_src}"))
    if cond.rh_in <= float(cfg["rh_floor"]):
        return False, f"dry boost off: RH at the {float(cfg['rh_floor']):.0f} % floor; {s}"
    if cond.t_min < float(cfg["min_room_c"]):
        return False, f"dry boost off: a room below {float(cfg['min_room_c']):.0f} °C; {s}"
    if active:
        if cond.delta < float(cfg["off_delta_c"]):
            if held < float(cfg["min_on_min"]):
                return True, f"dry boost held (min-on {float(cfg['min_on_min']) - held:.0f}m left); {s}"
            return False, f"dry boost ended: Δ below {float(cfg['off_delta_c']):.1f} °C; {s}"
        return True, f"dry boost: {s}"
    if cond.delta >= float(cfg["on_delta_c"]) and cond.rh_in >= float(cfg["rh_on"]):
        if held < float(cfg["min_off_min"]):
            return False, f"dry boost waiting (min-off {float(cfg['min_off_min']) - held:.0f}m left); {s}"
        return True, f"dry boost: {s}"
    return False, f"dry boost idle: {s}"
