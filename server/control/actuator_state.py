"""Shared actuator telemetry / area-stamping contract (ADR-0027).

ONE place stamps an actuator's `device_id` + `area` into its canonical `/state` payload, so the two
actuator families can't drift: the Midea dehumidifier (published by `ha-controller._publish_state`, polled
local driver) and the Levoit purifier (published by `levoit_bridge`, pushed ingest) both call
`resolve_area()` + `actuator_state()`. `area` is resolved from the reload-aware CONTROL registry
(`control.yaml`) by `device_id` — the SINGLE source (ADR-0027 §Decision), so a relocate edits one file and
every telemetry path follows via the live reload (`server/util/registry_reload.py`).

Pure + dependency-free so both the control loop and the ingest bridges can import it and it unit-tests
without a broker.
"""
from __future__ import annotations

from datetime import datetime, timezone

SCHEMA = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_area(control_registry, device_id: str, *, fallback: str | None = None) -> str:
    """The single actuator-area source: `control.yaml` (the control registry) keyed by `device_id`.
    `control_registry` is {device_id -> DeviceCtl|dict}. Returns the registry area, else `fallback`, else
    'unknown'. Callers pass a reload-aware registry so a relocate takes effect without a restart."""
    ctl = (control_registry or {}).get(device_id)
    if ctl is None:
        area = None
    elif isinstance(ctl, dict):
        area = ctl.get("area")
    else:
        area = getattr(ctl, "area", None)
    return area or fallback or "unknown"


def actuator_state(*, device_id: str, device_type: str, area: str, metrics: dict, transport: str,
                   ts: str | None = None, meta: dict | None = None, extra: dict | None = None):
    """Build the canonical actuator `(topic, payload)`. The common envelope
    (`schema, device_id, device_type, area, ts, transport, metrics, meta`) is stamped here for every
    family; `extra` merges family-specific top-level fields (e.g. Midea's `running`/`target_pct`), `meta`
    is the family meta block. `area` should come from `resolve_area()` so identity/location are
    single-sourced. Topic is `home/<area>/<device_id>/state`, matching the writer's denormalization."""
    payload = {
        "schema": SCHEMA,
        "device_id": device_id,
        "device_type": device_type,
        "area": area,
        "ts": ts or _utc_now(),
        "transport": transport,
        "metrics": dict(metrics or {}),
        "meta": dict(meta or {}),
    }
    if extra:
        payload.update(extra)
    return f"home/{area}/{device_id}/state", payload
