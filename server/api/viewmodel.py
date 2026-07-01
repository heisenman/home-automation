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
}


def metric_spec(metric: str) -> dict:
    """Presentation spec for a metric ({key,label,unit,color,precision,graph}). Unknown -> minimal default."""
    s = METRIC_CATALOG.get(metric)
    if s is None:
        return {"key": metric, "label": metric, "unit": "", "color": "#94a3b8", "precision": 1, "graph": False}
    return {"key": metric, **s}


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
        if m.get("hidden") or m.get("retired"):      # user-hidden or retired (R8 lifecycle) — drop from view
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
        e["age_s"] = _age_s(e["ts"], now)
        e["graphs"] = sensor_graphs(e["metrics"])    # shared UI spec: which metrics graph, +unit/color/label
    out.sort(key=lambda e: (e["room"], e["device_id"]))
    return out


# control INPUT metric per strategy (the value the loop drives on). Default = RH (hysteresis/setpoint).
_CONTROL_METRIC_BY_STRATEGY = {"threshold_ranged": "pm25_ugm3"}


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
        "room": dm.get("room") or None,
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
        "traits": traits,
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
