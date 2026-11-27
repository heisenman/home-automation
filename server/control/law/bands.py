"""The law's user settings (docs/design/unified-control-law.md §2): bands, cost basis, per-device enrolment.

"The law is an algorithm and the critical inputs are user-selected bands for temperature, RH, AQ (the last being
more fuzzy and fungible)" (Hugh, 2026-11-12). Temperature and RH bands are FIRM (leaving one is an error the law
must clear); the AQ band is SOFT (a floor with a weight, traded against cost).

Stored in control.db `house_setting` (control_store's JSON key/value): `law:bands`, `law:cost`, `law:enrol`.
Defaults mirror today's rules, so turning the shadow on changes nothing a person can feel.
"""
from __future__ import annotations

ROOMS = ["meter_pro_living_room", "meter_kitchen", "meter_c_bed", "meter_pro_c_office", "meter_pro_h_bed"]

BANDS_DEFAULT = {
    "temp_f": {"min": 68.0, "max": 76.0},       # house mean of `temp_sensors`
    "rh": {"min": 40.0, "max": 46.0, "dry_past": 1.0},   # on `rh_sensor` (the meter both dehumidifiers run
                                                # on); max = the lowest dehumidifier on-threshold today (Midea 46),
                                                # min = over-dry. Drying starts at max and runs to min − dry_past
                                                # (Hugh 2026-11-27: long runs, not short cycles; the meter reads
                                                # whole %, so 43 − 1 stops at a reading of 42)
    "aq": {"floor": 75.0, "weight": 1.0, "step": 15.0, "deadband": 5.0},   # air-quality index (0–100, lower =
                                                # worse); floor = the ERV's
                                                # level-2 band edge. weight = kWh/h a level of missing ventilation
                                                # is worth (soft); step = index points per level (15: level 3 at
                                                # <= 60, the ERV rule's own bands; Hugh 2026-11-19, was 10).
                                                # deadband = a running level holds within this many points of the
                                                # edge it would cross, the rules' ±5 (Hugh 2026-11-21)
    "rh_sensor": "meter_pro_living_room",
    "temp_sensors": ROOMS,
    "horizon_h": 3.0,                           # clear a firm band error over this many hours (the house buffers
                                                # water ~26× its air volume, so humidity moves in hours)
    "outdoor_drying": {"enabled": True, "on_dp_c": 1.5, "off_dp_c": 0.75, "restart_rh_pts": 1.0,
                       "cost_check": True, "cost_hyst": 0.2},   # Hugh 2026-11-17: while RH is at
                                                # or above the band's min and outdoor air is drier (dew-point gap,
                                                # the ERV dry boost's own thresholds), the ventilator runs at its
                                                # top level; below the band, no drying. Same weight as AQ (soft).
                                                # It stops at the band's min and restarts above min +
                                                # restart_rh_pts (Hugh 2026-11-19: 40 % stop, back above 41 %).
                                                # cost_check (Hugh 2026-11-20): in band, only while the extra
                                                # water is worth its fan + heat-loss cost at the cheapest
                                                # dehumidifier's price; held to 1 − cost_hyst of it once running
    "hold_h": 2.0,                              # a manual set of a law-driven device (PWA, or the thermostat's own
                                                # wall controls) holds it this long, or to the next goal-schedule
                                                # change if sooner (Hugh 2026-11-26)
    "schedules": [],                            # goal schedules (Hugh 2026-11-26: "one set for now, but schedules
                                                # will probably happen"): [{name, window "HH:MM-HH:MM", days?
                                                # [0=Mon..6], enabled, temp_f? {min,max}, rh? {min,max},
                                                # aq? {floor}}] — the first enabled match replaces those goals
}
MAX_SCHEDULES = 12
SCHEDULE_GOALS = ("temp_f", "rh", "aq")
COST_DEFAULT = {"basis": "kwh", "electric_per_kwh": None, "gas_per_therm": None}
ENROL_MODES = ("rule", "shadow", "law")         # rule = the device's own automation, left out of the comparison;
                                                # shadow (default) = its own automation, compared with the law;
                                                # law = the law drives it (stage L4: per device, Hugh's choice)
KEYS = {"bands": "law:bands", "cost": "law:cost", "enrol": "law:enrol"}


def _merge(base: dict, over: dict | None) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = {**base[k], **v} if isinstance(base.get(k), dict) and isinstance(v, dict) else v
    return out


def get(conn, store) -> dict:
    return {"bands": _merge(BANDS_DEFAULT, store.get_setting(conn, KEYS["bands"])),
            "cost": _merge(COST_DEFAULT, store.get_setting(conn, KEYS["cost"])),
            "enrol": dict(store.get_setting(conn, KEYS["enrol"]) or {})}


def enrolment(settings: dict, device_id: str) -> str:
    return settings["enrol"].get(device_id, "shadow")


def _hhmm(s: str) -> int:
    h, m = s.strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(s)
    return h * 60 + m


def _window(w: str) -> tuple[int, int]:
    a, b = w.split("-")
    return _hhmm(a), _hhmm(b)


def schedule_active(sched: dict, tod: int, weekday: int) -> bool:
    """Is a goal schedule on at minute-of-day `tod` on `weekday` (0 = Monday), house time? A window past midnight
    belongs to the day it starts on (a Friday 22:00–07:00 covers Saturday 02:00)."""
    if not sched.get("enabled", True):
        return False
    start, end = _window(sched["window"])
    if start == end:
        return False
    if start < end:
        on, day = start <= tod < end, weekday
    else:
        on, day = (tod >= start or tod < end), (weekday if tod >= start else (weekday - 1) % 7)
    days = sched.get("days")
    return on and (not days or day in days)


def effective(bands: dict, tod: int, weekday: int) -> tuple[dict, str | None]:
    """(the goals in force at that house time, the active schedule's name or None). The first enabled schedule
    that matches replaces the goals it names (temperature, RH, the AQ floor); everything else is the base set."""
    for sc in bands.get("schedules") or []:
        if schedule_active(sc, tod, weekday):
            out = dict(bands)
            for g in SCHEDULE_GOALS:
                if isinstance(sc.get(g), dict):
                    out[g] = {**bands[g], **sc[g]}
            return out, sc.get("name") or sc["window"]
    return bands, None


def house_clock(now: float, tz: str | None) -> tuple[int, int]:
    """(minute-of-day, weekday) of epoch `now` in the house zone."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from server.control.automation import HOUSE_TZ_DEFAULT, valid_tz
    lt = datetime.fromtimestamp(now, ZoneInfo(tz if valid_tz(tz) else HOUSE_TZ_DEFAULT))
    return lt.hour * 60 + lt.minute, lt.weekday()


def effective_now(conn, store, now: float, settings: dict | None = None) -> tuple[dict, str | None]:
    st = settings or get(conn, store)
    return effective(st["bands"], *house_clock(now, store.get_setting(conn, "timezone")))


def next_change(bands: dict, now: float, tz: str | None, horizon_s: float = 86400) -> float | None:
    """Epoch of the next minute the active goal schedule changes (None = no change within `horizon_s`)."""
    if not bands.get("schedules"):
        return None
    cur = effective(bands, *house_clock(now, tz))[1]
    t = (int(now) // 60 + 1) * 60
    while t - now <= horizon_s:
        if effective(bands, *house_clock(t, tz))[1] != cur:
            return float(t)
        t += 60
    return None


def _schedules_error(scheds) -> str | None:
    if not isinstance(scheds, list) or len(scheds) > MAX_SCHEDULES:
        return f"schedules must be a list of at most {MAX_SCHEDULES}"
    for i, sc in enumerate(scheds):
        where = f"schedules[{i}]"
        if not isinstance(sc, dict):
            return f"{where} must be an object"
        if not isinstance(sc.get("name", ""), str) or len(sc.get("name", "")) > 40:
            return f"{where}.name must be text (40 characters at most)"
        try:
            a, b = _window(str(sc.get("window", "")))
        except (ValueError, AttributeError):
            return f"{where}.window must be HH:MM-HH:MM"
        if a == b:
            return f"{where}.window must not start and end at the same time"
        days = sc.get("days")
        if days is not None and not (isinstance(days, list) and all(isinstance(d, int) and 0 <= d <= 6 for d in days)):
            return f"{where}.days must be a list of weekdays 0 (Mon) – 6 (Sun)"
        if not isinstance(sc.get("enabled", True), bool):
            return f"{where}.enabled must be true/false"
        if not any(isinstance(sc.get(g), dict) for g in SCHEDULE_GOALS):
            return f"{where} must set at least one goal (temp_f, rh or aq)"
        t, rh, aq = sc.get("temp_f"), sc.get("rh"), sc.get("aq")
        if t is not None and not (isinstance(t, dict) and _num(t.get("min"), 45, 90) and _num(t.get("max"), 50, 95)
                                  and t["min"] < t["max"]):
            return f"{where}.temp_f needs min < max, °F in 45–95"
        if rh is not None and not (isinstance(rh, dict) and _num(rh.get("min"), 20, 70) and _num(rh.get("max"), 25, 75)
                                   and rh["min"] < rh["max"]):
            return f"{where}.rh needs min < max, % in 20–75"
        if aq is not None and not (isinstance(aq, dict) and set(aq) <= {"floor"} and _num(aq.get("floor"), 0, 100)):
            return f"{where}.aq takes only floor, 0–100"
    return None


def _num(v, lo, hi) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


def validate(body: dict, current: dict) -> tuple[str | None, dict]:
    """(error, the settings after applying `body`). Pure."""
    b = body or {}
    new = {"bands": _merge(current["bands"], b.get("bands")), "cost": _merge(current["cost"], b.get("cost")),
           "enrol": {**current["enrol"], **(b.get("enrol") or {})}}
    bd, c = new["bands"], new["cost"]
    t, rh, aq = bd["temp_f"], bd["rh"], bd["aq"]
    if not (_num(t.get("min"), 45, 90) and _num(t.get("max"), 50, 95) and t["min"] < t["max"]):
        return "temp_f needs min < max, °F in 45–95", new
    if not (_num(rh.get("min"), 20, 70) and _num(rh.get("max"), 25, 75) and rh["min"] < rh["max"]):
        return "rh needs min < max, % in 20–75", new
    if not _num(rh.get("dry_past", 0.0), 0, 5):
        return "rh.dry_past must be 0–5 (% points drying runs past the band's bottom)", new
    if not (_num(aq.get("floor"), 0, 100) and _num(aq.get("weight"), 0, 10) and _num(aq.get("step"), 1, 50)
            and _num(aq.get("deadband", 0.0), 0, 20) and aq.get("deadband", 0.0) < aq["step"]):
        return "aq needs floor 0–100, weight 0–10, step 1–50, deadband 0–20 and below step", new
    if not _num(bd.get("horizon_h"), 0.5, 24):
        return "horizon_h must be 0.5–24", new
    if not _num(bd.get("hold_h"), 0.25, 24):
        return "hold_h must be 0.25–24 (hours a manual set holds a law-driven device)", new
    err = _schedules_error(bd.get("schedules"))
    if err:
        return err, new
    od = bd.get("outdoor_drying") or {}
    if not (isinstance(od.get("enabled"), bool) and _num(od.get("on_dp_c"), 0, 10) and _num(od.get("off_dp_c"), 0, 10)
            and od["off_dp_c"] <= od["on_dp_c"] and _num(od.get("restart_rh_pts"), 0, 10)
            and isinstance(od.get("cost_check", True), bool) and _num(od.get("cost_hyst", 0.0), 0, 0.9)):
        return ("outdoor_drying needs enabled true/false, off_dp_c <= on_dp_c (°C, 0–10), restart_rh_pts "
                "0–10, cost_check true/false and cost_hyst 0–0.9"), new
    if not (isinstance(bd.get("rh_sensor"), str) and isinstance(bd.get("temp_sensors"), list)
            and bd["temp_sensors"] and all(isinstance(x, str) for x in bd["temp_sensors"])):
        return "rh_sensor must be a sensor id and temp_sensors a non-empty list", new
    if c.get("basis") not in ("kwh", "money"):
        return "cost.basis must be 'kwh' or 'money'", new
    for k in ("electric_per_kwh", "gas_per_therm"):
        if c.get(k) is not None and not _num(c[k], 0, 20):
            return f"cost.{k} must be a price (0–20) or null", new
    if c["basis"] == "money" and (c.get("electric_per_kwh") is None or c.get("gas_per_therm") is None):
        return "cost basis 'money' needs electric_per_kwh and gas_per_therm", new
    for dev, mode in new["enrol"].items():
        if mode not in ENROL_MODES:
            return f"enrol[{dev}] must be one of {list(ENROL_MODES)}", new
    return None, new


def put(conn, store, body: dict, devices=None) -> tuple[int, dict]:
    """`devices` = the control registry's ids, when the caller has them: an enrolment must name a real device."""
    err, new = validate(body, get(conn, store))
    if not err and devices is not None:
        unknown = sorted(set((body or {}).get("enrol") or {}) - set(devices))
        if unknown:
            err = f"enrol names unknown device(s): {', '.join(unknown)}"
    if err:
        return 400, {"status": "bad-request", "reason": err}
    for part, key in KEYS.items():
        store.set_setting(conn, key, new[part])
    return 200, {"status": "ok", **new}
