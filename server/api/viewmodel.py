"""Display view-models (BFF) — ADR-0013 API-first presentation.

A constrained client (Seeed e-paper panel, phone widget, the web app's device card) shouldn't have to
stitch together control.db + hot.db + the resolver's vocabulary itself. This module composes ONE flat,
render-ready snapshot per controllable device: what it's doing, the authoritative reading driving it, the
device's own (non-authoritative) read, any active override, the last decision, and a single health word.

Pure functions over two sqlite connections (control.db + hot.db) so they unit-test without a web server.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from server.gas_compensation import air_quality_index, clean_air_baseline


def dewpoint_c(temp_c, rh_pct):
    """Dew point (°C) from temperature (°C) + relative humidity (%), Magnus-Tetens. None if undefined."""
    if temp_c is None or rh_pct is None or rh_pct <= 0:
        return None
    a, b = 17.625, 243.04
    g = math.log(rh_pct / 100.0) + a * temp_c / (b + temp_c)
    return round(b * g / (a - g), 1)


def _age_s(ts_iso: str | None, now: float) -> float | None:
    if not ts_iso:
        return None
    try:
        t = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, now - t.timestamp())
    except (ValueError, TypeError):
        return None


def _pending_active(m: dict, now: float) -> bool:
    """True while a device is on a migration pending-hold (meta.pending_until in the future): held OUT of
    the sensor list, so it neither shows in the GUI nor generates alerts/ntfy. When the window passes the
    sweeper drops it (or clears the hold if it reported fresh data). Malformed/absent → not pending."""
    pu = (m or {}).get("pending_until")
    if not pu:
        return False
    try:
        return now < datetime.fromisoformat(pu.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return False


# ── shared UI spec (ADR-0019 merge) ──────────────────────────────────────────────
# The SINGLE source of metric-presentation truth. Both renderers — the PWA (server/web
# /app.js) and the D1001 LVGL panel — render from this instead of hardcoding label/unit/
# color/precision client-side (the PWA's old `GRAPHABLE` constant). Order is the render
# order for value rows + chart stacks. `graph`: include in the chart set. Keep this the
# authoritative copy; the PWA falls back to its baked-in list only if the field is absent.
METRIC_CATALOG: dict[str, dict] = {
    "temperature_c": {"label": "Temperature", "unit": "°C",    "color": "#f87171", "precision": 1, "graph": True},
    "humidity_pct":  {"label": "Humidity",    "unit": "%RH",   "color": "#4aa3ff", "precision": 1, "graph": True},
    "dewpoint_c":    {"label": "Dew point",   "unit": "°C",    "color": "#22d3ee", "precision": 1, "graph": True},
    "co2_ppm":       {"label": "CO₂",         "unit": "ppm",   "color": "#fbbf24", "precision": 0, "graph": True},
    "radon_bqm3":    {"label": "Radon",       "unit": "Bq",    "color": "#a78bfa", "precision": 0, "graph": True},
    "pressure_hpa":  {"label": "Pressure",    "unit": "hPa",   "color": "#34d399", "precision": 0, "graph": True},
    "pm25_ugm3":     {"label": "PM2.5",       "unit": "µg/m³", "color": "#fb7185", "precision": 0, "graph": True},
    "aqi":           {"label": "AQI",         "unit": "",      "color": "#fbbf24", "precision": 0, "graph": True},
    "air_quality":   {"label": "Air Quality", "unit": "AQ",    "color": "#4ade80", "precision": 0, "graph": True},
    "voc_index":     {"label": "VOC Index",   "unit": "VOC",   "color": "#34d399", "precision": 0, "graph": True},
    "voc_raw":       {"label": "VOC (raw)",   "unit": "",      "color": "#6ee7b7", "precision": 0, "graph": True},
    "eco2":          {"label": "eCO₂",        "unit": "ppm",   "color": "#f59e0b", "precision": 0, "graph": True},
    "tvoc":          {"label": "TVOC",        "unit": "ppb",   "color": "#a3e635", "precision": 0, "graph": True},
    "power_w":       {"label": "Power",       "unit": "W",     "color": "#f59e0b", "precision": 0, "graph": True},
    "energy_today_kwh": {"label": "Energy today", "unit": "kWh", "color": "#eab308", "precision": 2, "graph": True},
}


# Core metrics stay in the 0b map tier (temp/hum are the climate through-line); secondary metrics (CO₂/PM/…)
# drop first when a room box can't fit everything (docs/design/map-room-fill.md — the dev half, item 3).
CORE_METRICS = {"temperature_c", "humidity_pct"}


def metric_spec(metric: str) -> dict:
    """Presentation spec for a metric ({key,label,unit,color,precision,graph,core}). Unknown -> minimal."""
    core = metric in CORE_METRICS
    s = METRIC_CATALOG.get(metric)
    if s is None:
        return {"key": metric, "label": metric, "unit": "", "color": "#94a3b8", "precision": 1,
                "graph": False, "core": core}
    return {"key": metric, "core": core, **s}


def ui_metric_catalog() -> list[dict]:
    """The full ordered metric catalog as a flat list (top-level `metrics` on /api/v1/sensors)."""
    return [metric_spec(k) for k in METRIC_CATALOG]


def sensor_graphs(metrics: dict) -> list[dict]:
    """The ordered graphable-metric spec list for the metrics a sensor actually reports. This is the
    server-authored equivalent of the PWA's `GRAPHABLE.filter(present)` — both renderers consume it."""
    return [metric_spec(k) for k, s in METRIC_CATALOG.items() if s["graph"] and metrics.get(k) is not None]


def _latest(hot, device_id: str, metric: str, authoritative: int):
    """Most recent (value, ts) for a metric at the given trust level, or None."""
    r = hot.execute(
        "SELECT value, ts FROM readings WHERE device_id=? AND metric=? AND authoritative=? "
        "ORDER BY ts DESC LIMIT 1", (device_id, metric, authoritative)).fetchone()
    return (r[0], r[1]) if r else None


# ── alerts (derived from the sensor list + device displays) ──────────────────────
LOW_BATTERY_PCT = 20         # warning below this
CRIT_BATTERY_PCT = 10        # critical below this
SENSOR_STALE_S = 1800        # 30 min with no reading -> unreachable
OVERRIDE_EXPIRING_S = 300    # warn when a manual override has <5 min left


def build_alerts(sensors: list[dict], displays: list[dict], now: float) -> list[dict]:
    """Active, actionable alerts derived from the current sensor list + device view-models. Pure.
    Severity: critical | warning | info. Each: {severity, kind, device_id, name, detail}."""
    out: list[dict] = []

    def label(o):
        return o.get("name") or o.get("device_id")

    for s in sensors or []:
        m = s.get("metrics") or {}
        b = m.get("battery_pct")
        if b is not None and b < LOW_BATTERY_PCT:
            out.append({"severity": "critical" if b < CRIT_BATTERY_PCT else "warning",
                        "kind": "low_battery", "device_id": s["device_id"], "name": label(s),
                        "detail": f"battery {round(b)}%"})
        age = s.get("age_s")
        if age is not None and age > SENSOR_STALE_S:
            out.append({"severity": "warning", "kind": "unreachable", "device_id": s["device_id"],
                        "name": label(s), "detail": f"no data for {round(age / 60)} min"})

    for d in displays or []:
        last = d.get("last_decision") or {}
        if last.get("source") == "safety" and "tank" in (last.get("reason") or "").lower():
            out.append({"severity": "critical", "kind": "tank_full", "device_id": d["device_id"],
                        "name": label(d), "detail": "tank full — dehumidifier paused"})
        ov = d.get("override")
        if ov and ov.get("expires_in_min") is not None and ov["expires_in_min"] * 60 < OVERRIDE_EXPIRING_S:
            out.append({"severity": "info", "kind": "override_expiring", "device_id": d["device_id"],
                        "name": label(d), "detail": f"{ov['action']} override ends in "
                        f"{max(0, round(ov['expires_in_min']))} min"})

    rank = {"critical": 0, "warning": 1, "info": 2}
    out.sort(key=lambda a: (rank.get(a["severity"], 3), a["device_id"]))
    return out


def _resolve_ambient_ref(gas: dict, devices: list[dict]) -> dict | None:
    """Auto-pick the reference T/H sensor for a self-heated gas node — the SYSTEM's own guess, NO config:
    the nearest reliable same-room sensor that reports both temperature + humidity and isn't itself a
    (self-heated) gas node. Prefers a 'pro' meter, then the freshest. Same area is the proximity proxy."""
    area = gas.get("area")
    best = None
    for d in devices:
        if d is gas or d.get("area") != area:
            continue
        dt = (d.get("device_type") or "").lower()
        if "gas" in dt or "bme680" in dt:                     # skip self-heated / gas nodes
            continue
        dm = d.get("metrics") or {}
        if dm.get("humidity_pct") is None or dm.get("temperature_c") is None:
            continue
        score = (1 if "pro" in d.get("device_id", "") else 0, d.get("ts") or "")
        if best is None or score > best[0]:
            best = (score, d)
    return best[1] if best else None


def _gas_baseline(hot_conn, device_id: str):
    """Clean-air gas-resistance baseline from recent history (bounded window). See clean_air_baseline()."""
    rows = hot_conn.execute(
        "SELECT value FROM readings WHERE device_id=? AND metric='gas_ohm' AND authoritative=1 "
        "ORDER BY ts DESC LIMIT 720", (device_id,)).fetchall()
    return clean_air_baseline([r[0] for r in rows])


def _compensate_gas_nodes(devices: list[dict], hot_conn) -> None:
    """BME680 gas nodes self-heat, so their own temp/RH aren't valid ambient — HIDE them, and derive a
    humidity-compensated `air_quality` (0..100, higher = cleaner) from the auto-picked reference sensor's
    TRUE humidity. No per-node config: the reference is resolved from live data. If no reference/gas data
    is available we still hide the bad T/RH so the UI never shows a misleading self-heated number."""
    for gas in devices:
        if (gas.get("device_type") or "") != "bme680_gas":
            continue
        gm = gas["metrics"]
        gas_ohm = gm.get("gas_ohm")
        ref = _resolve_ambient_ref(gas, devices)
        rh = (ref.get("metrics") or {}).get("humidity_pct") if ref else None
        if gas_ohm is not None and rh is not None:
            base = _gas_baseline(hot_conn, gas["device_id"]) or gas_ohm
            gm["air_quality"] = air_quality_index(gas_ohm, rh, base)["air_quality"]
            gas["ambient_ref"] = ref["device_id"]             # surfaced for transparency
        for bad in ("temperature_c", "humidity_pct", "dewpoint_c"):
            gm.pop(bad, None)                                 # self-heated / derived-from-self-heated → drop
        gas["graphs"] = sensor_graphs(gm)                     # rebuild after mutating metrics


def build_sensor_list(hot_conn, now: float, meta: dict | None = None,
                      calib: dict | None = None) -> list[dict]:
    """All TRUSTED sensors with their latest value per metric, grouped per device (one query). Device
    self-reports (authoritative=0, e.g. the dehumidifier's onboard RH) are excluded — they live in the
    control view, not the sensor view. Sorted by (overlay) room then device_id. `meta` is the user overlay
    {device_id: {name, room, hidden}} (ADR-0014 R8): hidden devices are dropped, name/room surfaced."""
    if hot_conn is None:
        return []
    meta = meta or {}
    rows = hot_conn.execute(
        """SELECT r.device_id, r.metric, r.value, r.ts, d.device_type, d.area
             FROM readings r
             JOIN (SELECT device_id, metric, MAX(ts) AS mts FROM readings
                   WHERE authoritative=1 GROUP BY device_id, metric) m
               ON r.device_id=m.device_id AND r.metric=m.metric AND r.ts=m.mts
             LEFT JOIN device_last_seen d ON d.device_id=r.device_id
            WHERE r.authoritative=1""").fetchall()
    by_dev: dict[str, dict] = {}
    for did, metric, value, ts, dtype, area in rows:
        if did.startswith("unknown"):       # unregistered MAC the scanner saw — not a user device; hide
            continue
        m = meta.get(did) or {}
        if m.get("hidden") or m.get("retired") or _pending_active(m, now):   # hidden/retired/pending-hold → drop from view
            continue
        e = by_dev.setdefault(did, {"device_id": did, "device_type": dtype or "unknown",
                                    "area": area or "unknown", "ts": ts, "metrics": {}})
        e["metrics"][metric] = value
        if ts and ts > e["ts"]:
            e["ts"] = ts
    calib = calib or {}
    out = list(by_dev.values())
    for e in out:
        m = meta.get(e["device_id"]) or {}
        offs = calib.get(e["device_id"]) or {}
        for metric, off in offs.items():            # display-only offset (control reads raw MQTT)
            if metric in e["metrics"]:
                e["metrics"][metric] = e["metrics"][metric] + off
        e["offsets"] = offs
        # derived: dew point for any sensor reporting both temp + RH (often more useful than RH alone)
        dp = dewpoint_c(e["metrics"].get("temperature_c"), e["metrics"].get("humidity_pct"))
        if dp is not None:
            e["metrics"]["dewpoint_c"] = dp
        e["name"] = m.get("name") or None           # UI falls back to a prettified device_id
        e["room"] = m.get("room") or e["area"]      # overlay room wins; else the registry area
        e["climate_role"] = m.get("climate_role")   # primary|secondary|None -> room climate resolver
        e["age_s"] = _age_s(e["ts"], now)
        e["graphs"] = sensor_graphs(e["metrics"])    # shared UI spec: which metrics graph, +unit/color/label
    _compensate_gas_nodes(out, hot_conn)             # BME680: hide self-heated T/RH, derive air_quality (fusion)
    out.sort(key=lambda e: (e["room"], e["device_id"]))
    return out


def build_actuator_list(hot_conn, control_registry, present_ids, *, meta=None, now=None) -> list[dict]:
    """Sensor-shaped entries for controllable devices (actuators) the authoritative sensor path did NOT
    already surface — e.g. the Midea dehumidifier, whose self-reported telemetry is non-authoritative
    (the writer demotes transport='midea-lan'), so it never appears via build_sensor_list and would
    otherwise vanish from the room graph entirely.

    Area/type come from the CONTROL REGISTRY (control.yaml) — the canonical source — NOT device_last_seen,
    which a (possibly stale) controller may have stamped 'unknown'. Latest metric values are pulled at
    EITHER trust level so the tile still shows current RH/target. An actuator whose area isn't a canonical
    room falls through build_rooms into `unlocated` (the red flag) rather than disappearing. Pairs with
    build_rooms; `present_ids` is the set of device_ids already emitted by build_sensor_list (skip those)."""
    meta = meta or {}
    present = present_ids or set()
    out = []
    for did, ctl in (control_registry or {}).items():
        if did in present:
            continue
        m = meta.get(did) or {}
        if m.get("hidden") or m.get("retired"):            # honour the R8 hide/retire lifecycle
            continue
        area = ctl.get("area") if isinstance(ctl, dict) else getattr(ctl, "area", None)
        dtype = ctl.get("device_type") if isinstance(ctl, dict) else getattr(ctl, "device_type", None)
        metrics: dict = {}
        ts = None
        if hot_conn is not None:
            for metric, value, rts in hot_conn.execute(
                    """SELECT r.metric, r.value, r.ts FROM readings r
                         JOIN (SELECT metric, MAX(ts) AS mts FROM readings
                                WHERE device_id=? GROUP BY metric) x
                           ON r.metric=x.metric AND r.ts=x.mts
                        WHERE r.device_id=?""", (did, did)).fetchall():
                metrics[metric] = value
                if rts and (ts is None or rts > ts):
                    ts = rts
        dp = dewpoint_c(metrics.get("temperature_c"), metrics.get("humidity_pct"))
        if dp is not None:
            metrics["dewpoint_c"] = dp
        out.append({
            "device_id": did, "device_type": dtype or "unknown", "area": area or "unknown",
            "name": m.get("name") or None, "metrics": metrics, "ts": ts, "offsets": {},
            "room": m.get("room") or area or "unknown",
            "age_s": _age_s(ts, now) if now is not None else None,
            "graphs": sensor_graphs(metrics),
        })
    return out


# ── room-fill BFF content (docs/design/map-room-fill.md) — the panel authors fit, the BFF authors content ──
# Normal ranges: the server is the range authority; the panel just colors an out-of-range value red.
NORMAL_RANGES: dict[str, tuple[float, float]] = {
    "temperature_c": (16.0, 27.0),
    "humidity_pct":  (30.0, 60.0),
    "co2_ppm":       (0.0, 1000.0),
    "pm25_ugm3":     (0.0, 35.0),
    "voc_index":     (0.0, 250.0),
    "radon_bqm3":    (0.0, 150.0),
    "aqi":           (0.0, 100.0),
}
CLIMATE_STALE_S = 1800          # a configured primary/secondary climate sensor older than this falls through
CLIMATE_DIVERGE_C = 2.0         # room temperature spread above this -> averaged_divergent (an avg hides a spread)
CLIMATE_DIVERGE_RH = 10.0       # room humidity spread above this -> averaged_divergent


def _out_of_range(metric: str, value) -> bool:
    r = NORMAL_RANGES.get(metric)
    return bool(r) and value is not None and (value < r[0] or value > r[1])


def out_of_range_map(metrics: dict) -> dict:
    """Sparse {metric: True} for a device's out-of-range metrics (panel colors these red)."""
    return {k: True for k, v in (metrics or {}).items() if _out_of_range(k, v)}


def _mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else None


def resolve_room_climate(room_sensors: list[dict]) -> dict | None:
    """Per-room climate summary (docs/design/map-room-fill.md §Climate representative):
    `{value:{temperature_c,humidity_pct}, confidence, source_device_id}` or None if the room reports no
    temperature. `confidence`: a configured+fresh `climate_role` primary → `primary`; else its secondary →
    `secondary`; else the mean of the room's sensors → `averaged`, escalated to `averaged_divergent` when
    the sensors spread past the threshold (an average hiding a real spread is the thing worth flagging).
    Pure over sensor dicts (uses `age_s` for staleness, so no clock needed)."""
    temp_sensors = [s for s in room_sensors if (s.get("metrics") or {}).get("temperature_c") is not None]
    if not temp_sensors:
        return None

    def _fresh(s):
        a = s.get("age_s")
        return a is None or a <= CLIMATE_STALE_S

    def _val(s):
        m = s.get("metrics") or {}
        return {"temperature_c": m.get("temperature_c"), "humidity_pct": m.get("humidity_pct")}

    for role in ("primary", "secondary"):
        picked = next((s for s in temp_sensors if s.get("climate_role") == role and _fresh(s)), None)
        if picked:
            return {"value": _val(picked), "confidence": role, "source_device_id": picked["device_id"]}

    temps = [(s.get("metrics") or {}).get("temperature_c") for s in temp_sensors]
    hums = [h for s in temp_sensors if (h := (s.get("metrics") or {}).get("humidity_pct")) is not None]
    spread_t = (max(temps) - min(temps)) if len(temps) > 1 else 0.0
    spread_h = (max(hums) - min(hums)) if len(hums) > 1 else 0.0
    divergent = spread_t > CLIMATE_DIVERGE_C or spread_h > CLIMATE_DIVERGE_RH
    return {"value": {"temperature_c": _mean(temps), "humidity_pct": _mean(hums)},
            "confidence": "averaged_divergent" if divergent else "averaged",
            "source_device_id": None}


def actuator_map_state(display_vm: dict) -> dict:
    """`{running, status}` for an actuator on the spatial house map (docs/design/map-room-fill.md Arc 2),
    derived from a `build_display` view-model — so the map, room-zoom, and PWA agree (control registry is the
    source). `status` is a short, family-agnostic ACTION detail the panel renders verbatim (it does NOT
    re-derive per family): dehumidifier → target RH (e.g. "45%"); purifier → fan level (e.g. "Low"/"High",
    from the ranged trait labels); plug/switch → "on" when running else "". Not its onboard reading — the
    room climate line already covers ambient."""
    act = display_vm.get("actuator") or {}
    traits = display_vm.get("traits") or {}
    running = bool(display_vm.get("running"))
    if act.get("target_pct") is not None:
        status = f"{round(act['target_pct'])}%"
    elif act.get("fan_speed") is not None:
        speed = round(act["fan_speed"])
        rcfg = traits.get("ranged")
        label = next((o["label"] for o in _ranged_options(rcfg) if o["value"] == speed), None) if rcfg else None
        # named levels (Low/Med/High for 2-3 speeds) read verbatim; numeric levels get a "Fan N" prefix
        status = label if (label and not label.isdigit()) else f"Fan {speed}"
    elif running:
        status = "on"
    else:
        status = ""
    return {"running": running, "status": status}


def build_rooms(sensors: list[dict], areas: dict | None = None, geometry: dict | None = None,
                placement: dict | None = None, controllable_ids: set | None = None) -> dict:
    """The canonical room graph (ADR-0026 Phase 3 UI) — supersedes the bare `/areas` string list.

    Groups the live `sensors` (from build_sensor_list) into the canonical `areas` registry, attaching
    per-room geometry and per-device in-room placement so a renderer can draw the house map + room zoom
    without stitching four sources itself.

    Inputs (all optional; a missing file just degrades that facet, never errors):
      areas       {id: {name, level, zone, type}} in file order (instance/areas.yaml → `areas:`).
      geometry    {"rooms": {id: {...polygon/label/flags...}}} (instance/house-geometry.json).
      placement   {device_id: {x, y, anchor}} (instance/device-placement.yaml → `placements:`).
      controllable_ids  device_ids in the control registry → role "actuator" (else "sensor").

    Returns {schema_version, levels[], zones[], rooms[], unlocated[]}. `rooms` covers EVERY canonical
    area (incl. empty ones, ordered by file order); `unlocated` holds any device whose room isn't a
    canonical area (== "make sure all devices are properly located" — this list should be empty)."""
    from collections import defaultdict

    areas = areas or {}
    geo_rooms = (geometry or {}).get("rooms", {}) or {}
    placement = placement or {}
    ctl = controllable_ids or set()

    by_room: dict[str, list] = defaultdict(list)
    climate_inputs: dict[str, list] = defaultdict(list)   # per-room SENSOR dicts, for the climate summary
    unlocated: list[dict] = []
    for s in sensors:
        room_id = s.get("room") or s.get("area") or "unknown"

        def _dev(rid: str) -> dict:
            return {
                "device_id": s["device_id"],
                "device_type": s.get("device_type"),
                "role": "actuator" if s["device_id"] in ctl else "sensor",
                "name": s.get("name"),
                "metrics": s.get("metrics", {}),
                "out_of_range": out_of_range_map(s.get("metrics")),   # sparse {metric: True} -> panel reds
                "state": s.get("state"),                              # actuators: {running, status}; else null
                "age_s": s.get("age_s"),
                "placement": placement.get(s["device_id"]) or {"x": None, "y": None, "anchor": "auto"},
            }

        if room_id in areas:
            by_room[room_id].append(_dev(room_id))
            if s["device_id"] not in ctl:                 # climate is a SENSOR reading, not actuator onboard
                climate_inputs[room_id].append(s)
        else:
            # not a canonical area → NEVER silently drop it; surface it as a red flag the UI must act on
            # (a device that exists but has no valid location). `location_unknown` is the explicit signal.
            unlocated.append({**_dev(room_id), "area": room_id, "location_unknown": True,
                              "reason": ("device has no resolved location"
                                         if not room_id or room_id == "unknown"
                                         else f"area '{room_id}' is not a canonical room in areas.yaml")})

    rooms = []
    for order, (aid, meta) in enumerate(areas.items()):
        # stable device-line order so the greedy fit + PWA agree on what drops first: actuators, then
        # sensors, each by device_id (docs/design/map-room-fill.md — the dev half, item 4).
        devs = sorted(by_room.get(aid, []), key=lambda d: (d["role"] != "actuator", d["device_id"]))
        rooms.append({
            "id": aid,
            "name": (meta or {}).get("name", aid),
            "level": (meta or {}).get("level"),
            "zone": (meta or {}).get("zone"),
            "type": (meta or {}).get("type"),
            "order": order,
            "geometry": geo_rooms.get(aid),                     # null for rooms without a polygon (yet)
            "climate": resolve_room_climate(climate_inputs.get(aid, [])),   # {value,confidence,source} or null
            "devices": devs,
            # "edge" bucket stays 0 until a node-census source exists (edge nodes aren't in the sensor roster)
            "counts": {"sensors": sum(d["role"] == "sensor" for d in devs),
                       "actuators": sum(d["role"] == "actuator" for d in devs),
                       "edge": 0},
        })

    levels: list[str] = []
    zones: list[str] = []
    for meta in areas.values():
        lv, zn = (meta or {}).get("level"), (meta or {}).get("zone")
        if lv and lv not in levels:
            levels.append(lv)
        if zn and zn not in zones and zn != "none":
            zones.append(zn)

    return {"schema_version": 1, "levels": levels, "zones": zones,
            "rooms": rooms, "unlocated": unlocated}


# control INPUT metric per strategy (the value the loop drives on). Default = RH (hysteresis/setpoint).
_CONTROL_METRIC_BY_STRATEGY = {"threshold_ranged": "pm25_ugm3"}


# ── Controls spec (shared-ui-spec contract, Phase 0) — server-authored, render-ready control descriptors.
# Both renderers (PWA `app.js` + D1001 LVGL panel) iterate `vm.controls` and switch on `kind`; neither
# re-derives ranges, labels, admin-gating, or the action contract. Mirrors the client logic this replaces
# (`app.js` OverrideControls/ManualControl) and the ranges `traits.validate_command` enforces (server stays
# the authority). Raw `traits` is kept on the vm during migration for back-compat. ──
_RANGED_LEVEL_LABELS = {2: ["Low", "High"], 3: ["Low", "Med", "High"]}   # else numeric — matches app.js
_CMD_PATH = "/devices/{id}/command"


def _ranged_options(cfg: dict) -> list[dict]:
    """Enumerate a ranged trait's levels → [{value,label}], identical to app.js range()/steps()."""
    lo, hi = int(cfg.get("min", 0)), int(cfg.get("max", 100))
    step = cfg.get("step")
    vals = list(range(lo, hi + 1, int(step))) if step else list(range(lo, hi + 1))   # inclusive of hi
    names = _RANGED_LEVEL_LABELS.get(len(vals))
    return [{"value": v, "label": names[i] if names else str(v)} for i, v in enumerate(vals)]


def _setpoint_label(cfg: dict) -> tuple[str, str]:
    unit = cfg.get("unit", "")
    if unit in ("%", "%RH", "pct", "percent"):
        return "Target humidity", "%"
    if unit in ("degC", "°C", "C", "celsius"):
        return "Target temperature", "°C"
    return "Setpoint", unit


def build_controls(traits_cfg: dict | None) -> list[dict]:
    """Ordered, render-ready control descriptors for a controllable device (shared-ui-spec Phase 0).
    `override` is always present (every build_display device has a control policy); setpoint/ranged/
    indicator are emitted iff the device's traits_cfg declares that trait. Ranges/labels/admin/action are
    fully specified so both renderers stop deriving them client-side. A per-trait cfg `label` overrides
    the default (future device needs no client change)."""
    controls: list[dict] = [{
        "kind": "override", "label": "Power", "admin": True,
        "action": {"method": "POST", "path": "/control/{id}/override"},
        "presets": [
            {"action": "off",      "duration_min": 60, "label": "Off 1h"},
            {"action": "boost_on", "duration_min": 60, "label": "Boost 1h"},
            {"action": "clear",    "label": "Resume auto", "when": "override_active"},
        ],
        "state": {"from": "override"},
    }]
    tc = traits_cfg or {}
    if "setpoint" in tc:
        cfg = tc.get("setpoint") or {}
        label, unit = _setpoint_label(cfg)
        c = {"kind": "setpoint", "trait": "setpoint", "label": cfg.get("label") or label, "unit": unit,
             "admin": True, "now_key": "target_pct",
             "action": {"method": "POST", "path": _CMD_PATH, "trait": "setpoint", "action": "set",
                        "arg_key": "value"}}
        for k in ("min", "max", "safe_value"):
            if k in cfg:
                c[k] = cfg[k]
        controls.append(c)
    if "ranged" in tc:
        cfg = tc.get("ranged") or {}
        controls.append({
            "kind": "ranged", "trait": "ranged", "label": cfg.get("label") or "Fan speed", "admin": True,
            "options": _ranged_options(cfg), "now_key": "fan_speed",
            "action": {"method": "POST", "path": _CMD_PATH, "trait": "ranged", "action": "set",
                       "arg_key": "level"}})
    if "indicator" in tc:
        cfg = tc.get("indicator") or {}
        controls.append({
            "kind": "indicator", "trait": "indicator", "label": cfg.get("label") or "LED / panel light",
            "admin": True, "now_key": "led_on",
            "action": {"method": "POST", "path": _CMD_PATH, "trait": "indicator", "action": "set",
                       "arg_key": "on"}})
    return controls


def _device_area(hot_conn, device_id: str) -> str | None:
    """The device's current area from device_last_seen — the room fallback when there's no meta overlay.
    Mirrors build_sensor_list's `meta.room or area` so a controllable device lands in a room the same way
    a sensor does (else it renders room=None and gets filtered out of every room-zoom view)."""
    if hot_conn is None:
        return None
    try:
        r = hot_conn.execute("SELECT area FROM device_last_seen WHERE device_id=?", (device_id,)).fetchone()
        return r[0] if r and r[0] else None
    except Exception:
        return None


def build_display(control_conn, hot_conn, device_id: str, now: float, registry=None,
                  meta: dict | None = None) -> dict | None:
    """Compose the display view-model for one controllable device. None if it has no control policy.
    `registry` (device_id -> DeviceCtl), when supplied, adds the device's command capabilities (traits)
    so the UI can render manual controls. `meta` is the user overlay (R8): friendly name / room."""
    from server.api.control import read_control_state

    snap = read_control_state(control_conn, device_id, now)
    policy = snap["policy"]
    if policy is None:
        return None
    ctrl = policy.get("control", {}) or {}
    source_id = policy.get("source_sensor")
    # the metric this device controls on: an explicit control.metric (e.g. air-quality pm25_ugm3 vs aqi)
    # wins; otherwise default by strategy (threshold_ranged -> PM2.5, hysteresis/setpoint -> RH).
    metric = ctrl.get("metric") or _CONTROL_METRIC_BY_STRATEGY.get(ctrl.get("strategy"), "humidity_pct")

    def _latest_any(dev, m):                      # newest at either trust level (auth=1 sensor, =0 self-report)
        return _latest(hot_conn, dev, m, 1) or _latest(hot_conn, dev, m, 0) if hot_conn is not None else None

    # the authoritative reading that DRIVES the loop. Carries both the metric-named key (back-compat for
    # the dehumidifier card) and a generic value/metric so a purifier card can render the same shape.
    sensor = None
    if source_id and hot_conn is not None:
        sv = _latest(hot_conn, source_id, metric, 1)
        if sv:
            sensor = {"device_id": source_id, metric: sv[0], "value": sv[0], "metric": metric,
                      "ts": sv[1], "age_s": _age_s(sv[1], now)}

    # the device's OWN non-authoritative reading (dehumidifier: onboard RH runs low; purifier: self-sourced
    # PM2.5 is authoritative, so this is typically None — there is no separate untrusted onboard sensor).
    onboard = None
    if hot_conn is not None:
        ov = _latest(hot_conn, device_id, metric, 0)
        if ov:
            onboard = {metric: ov[0], "value": ov[0], "metric": metric, "ts": ov[1]}

    # current actuator telemetry (device setpoint + fan on/off + fan level). The controller demotes Midea
    # self-reports to auth=0; the Levoit bridge publishes auth=1 — so read at either level.
    actuator = {}
    if hot_conn is not None:
        tv = _latest(hot_conn, device_id, "target_humidity_pct", 0)
        fv = _latest_any(device_id, "fan_speed")
        fo = _latest_any(device_id, "fan_on")
        led = _latest_any(device_id, "led_on")
        if tv:
            actuator["target_pct"] = tv[0]
        if fv:
            actuator["fan_speed"] = fv[0]
        if fo:
            actuator["fan_on"] = fo[0]
        if led is not None:
            actuator["led_on"] = bool(led[0])

    # command capabilities (traits + ranges) so the UI can render manual controls
    traits = None
    if registry is not None:
        ctl = registry.get(device_id)
        if ctl is not None:
            traits = getattr(ctl, "traits_cfg", None)

    # running state: the latest tick logged res.running, which mirrors the live device status each tick
    last = snap["last_decision"]
    running = bool(last["desired"]) if last else None

    stale_s = float(policy.get("sensor_stale_min", 10)) * 60.0
    if not policy.get("enabled", True):
        health = "disabled"
    elif snap["override"] is not None:
        health = "overridden"
    elif sensor is None or (sensor["age_s"] is not None and sensor["age_s"] > stale_s):
        health = "stale"
    else:
        health = "ok"

    dm = (meta or {}).get(device_id) or {}
    return {
        "schema": 1,
        "device_id": device_id,
        "name": dm.get("name") or None,
        "room": dm.get("room") or _device_area(hot_conn, device_id),   # overlay wins; else the device's area
        "running": running,
        "control": {
            "enabled": bool(policy.get("enabled", True)),
            "strategy": ctrl.get("strategy", "hysteresis"),
            "metric": metric,                          # which sensor metric drives the loop (RH | pm25_ugm3)
            "on_above": ctrl.get("on_above"),
            "off_below": ctrl.get("off_below"),
            "bands": ctrl.get("bands"),                # threshold_ranged: sensor band -> fan speed
            "source_sensor": source_id,
            "fallback_sensors": policy.get("fallback_sensors") or [],
        },
        "sensor": sensor,
        "onboard": onboard,
        "actuator": actuator,
        "traits": traits,                              # raw — kept for back-compat during migration
        "controls": build_controls(traits),            # shared-ui-spec: server-authored render-ready controls
        "recent_decisions": [{"ts": r["ts"], "source": r["source"], "reason": r["reason"],
                              "acted": r["acted"]} for r in (snap.get("recent_log") or [])[:8]],
        "override": snap["override"],
        # house scene (Home/Away/Sleep): the active scene, this device's scene profiles (for the editor),
        # and what the active scene currently does to this device (parks it / relaxed thresholds).
        "scene": snap.get("scene"),
        "scenes": policy.get("scenes") or {},
        "scene_active": snap.get("scene_active"),
        "last_decision": ({"source": last["source"], "reason": last["reason"], "ts": last["ts"]}
                          if last else None),
        "health": health,
    }
