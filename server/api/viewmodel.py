"""Display view-models (BFF) — ADR-0013 API-first presentation.

A constrained client (Seeed e-paper panel, phone widget, the web app's device card) shouldn't have to
stitch together control.db + hot.db + the resolver's vocabulary itself. This module composes ONE flat,
render-ready snapshot per controllable device: what it's doing, the authoritative reading driving it, the
device's own (non-authoritative) read, any active override, the last decision, and a single health word.

Pure functions over two sqlite connections (control.db + hot.db) so they unit-test without a web server.
"""
from __future__ import annotations

from datetime import datetime, timezone


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


def _latest(hot, device_id: str, metric: str, authoritative: int):
    """Most recent (value, ts) for a metric at the given trust level, or None."""
    r = hot.execute(
        "SELECT value, ts FROM readings WHERE device_id=? AND metric=? AND authoritative=? "
        "ORDER BY ts DESC LIMIT 1", (device_id, metric, authoritative)).fetchone()
    return (r[0], r[1]) if r else None


def build_sensor_list(hot_conn, now: float) -> list[dict]:
    """All TRUSTED sensors with their latest value per metric, grouped per device (one query). Device
    self-reports (authoritative=0, e.g. the dehumidifier's onboard RH) are excluded — they live in the
    control view, not the sensor view. Sorted by area then device_id."""
    if hot_conn is None:
        return []
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
        e = by_dev.setdefault(did, {"device_id": did, "device_type": dtype or "unknown",
                                    "area": area or "unknown", "ts": ts, "metrics": {}})
        e["metrics"][metric] = value
        if ts and ts > e["ts"]:
            e["ts"] = ts
    out = list(by_dev.values())
    for e in out:
        e["age_s"] = _age_s(e["ts"], now)
    out.sort(key=lambda e: (e["area"], e["device_id"]))
    return out


def build_display(control_conn, hot_conn, device_id: str, now: float, registry=None) -> dict | None:
    """Compose the display view-model for one controllable device. None if it has no control policy.
    `registry` (device_id -> DeviceCtl), when supplied, adds the device's command capabilities (traits)
    so the UI can render manual controls."""
    from server.api.control import read_control_state

    snap = read_control_state(control_conn, device_id, now)
    policy = snap["policy"]
    if policy is None:
        return None
    ctrl = policy.get("control", {}) or {}
    source_id = policy.get("source_sensor")

    # the authoritative reading that DRIVES the loop (a trusted meter, not the device's own sensor)
    sensor = None
    if source_id and hot_conn is not None:
        sv = _latest(hot_conn, source_id, "humidity_pct", 1)
        if sv:
            sensor = {"device_id": source_id, "humidity_pct": sv[0], "ts": sv[1],
                      "age_s": _age_s(sv[1], now)}

    # the device's OWN reading (non-authoritative — runs ~9-15% low; shown, never trusted for control)
    onboard = None
    if hot_conn is not None:
        ov = _latest(hot_conn, device_id, "humidity_pct", 0)
        if ov:
            onboard = {"humidity_pct": ov[0], "ts": ov[1]}

    # current actuator telemetry (device setpoint + fan), published non-authoritative by the controller
    actuator = {}
    if hot_conn is not None:
        tv = _latest(hot_conn, device_id, "target_humidity_pct", 0)
        fv = _latest(hot_conn, device_id, "fan_speed", 0)
        if tv:
            actuator["target_pct"] = tv[0]
        if fv:
            actuator["fan_speed"] = fv[0]

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

    return {
        "schema": 1,
        "device_id": device_id,
        "running": running,
        "control": {
            "enabled": bool(policy.get("enabled", True)),
            "strategy": ctrl.get("strategy", "hysteresis"),
            "on_above": ctrl.get("on_above"),
            "off_below": ctrl.get("off_below"),
            "source_sensor": source_id,
        },
        "sensor": sensor,
        "onboard": onboard,
        "actuator": actuator,
        "traits": traits,
        "override": snap["override"],
        "last_decision": ({"source": last["source"], "reason": last["reason"], "ts": last["ts"]}
                          if last else None),
        "health": health,
    }
