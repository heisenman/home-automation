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
    "rh": {"min": 40.0, "max": 46.0},           # on `rh_sensor` (the meter both dehumidifiers run on); max = the
                                                # lowest dehumidifier on-threshold today (Midea 46), min = over-dry
    "aq": {"floor": 75.0, "weight": 1.0, "step": 10.0},   # air-quality index (0–100, lower = worse); the ERV's
                                                # level-2 band edge. weight = kWh/h a level of missing ventilation
                                                # is worth (soft); step = index points per level
    "rh_sensor": "meter_pro_living_room",
    "temp_sensors": ROOMS,
    "horizon_h": 3.0,                           # clear a firm band error over this many hours (the house buffers
                                                # water ~26× its air volume, so humidity moves in hours)
}
COST_DEFAULT = {"basis": "kwh", "electric_per_kwh": None, "gas_per_therm": None}
ENROL_MODES = ("rule", "shadow")                # "law" is stage L4: per device, Hugh's approval
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
    if not (_num(aq.get("floor"), 0, 100) and _num(aq.get("weight"), 0, 10) and _num(aq.get("step"), 1, 50)):
        return "aq needs floor 0–100, weight 0–10, step 1–50", new
    if not _num(bd.get("horizon_h"), 0.5, 24):
        return "horizon_h must be 0.5–24", new
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
            return (f"enrol[{dev}] must be one of {list(ENROL_MODES)} ('law' is stage L4: per device, with "
                    "Hugh's approval)"), new
    return None, new


def put(conn, store, body: dict) -> tuple[int, dict]:
    err, new = validate(body, get(conn, store))
    if err:
        return 400, {"status": "bad-request", "reason": err}
    for part, key in KEYS.items():
        store.set_setting(conn, key, new[part])
    return 200, {"status": "ok", **new}
