"""Thermostat comfort range, settable from the PWA (Hugh, 2026-11-07: "Let me set it. In the thermostat config.")

Each setpoint channel (heat, cool) has a MANUAL range (what a person may set) and a narrower `comfort` range: a
setpoint the current mode uses outside it raises the comfort warning (server/api/viewmodel.comfort_alert,
ADR-0043 §4). The registry's `comfort` is the default; a person's choice is a house setting in control.db
(`comfort:<device_id>` = {channel: {min, max}}), so it rides the standby snapshot like the plug power mode
(server/control/plug_power.py). Read-side only: the controller never reads the trait's comfort.
"""
from __future__ import annotations

import copy


def _key(device_id: str) -> str:
    return f"comfort:{device_id}"


def _channels(traits_cfg) -> dict:
    sp = (traits_cfg or {}).get("setpoint")
    return (sp or {}).get("channels") or {} if isinstance(sp, dict) else {}


def with_overrides(conn, traits_cfg, device_id: str):
    """traits_cfg with each channel's `comfort` replaced by the selected range (a copy; unchanged if none)."""
    chans = _channels(traits_cfg)
    if not chans or conn is None:
        return traits_cfg
    from server.control import control_store as store
    try:
        sel = store.get_setting(conn, _key(device_id)) or {}
    except Exception:
        return traits_cfg
    sel = {k: v for k, v in sel.items() if k in chans and isinstance(v, dict)}
    if not sel:
        return traits_cfg
    out = copy.deepcopy(traits_cfg)
    for k, v in sel.items():
        out["setpoint"]["channels"][k] = {**(out["setpoint"]["channels"][k] or {}),
                                          "comfort": {"min": v["min"], "max": v["max"]}}
    return out


def handle_set(conn, registry, device_id: str, body: dict) -> tuple[int, dict]:
    """PUT body {channel, min, max} sets one channel's comfort range; {channel, reset: true} returns it to the
    registry default. The range must sit inside the channel's manual range, min < max."""
    from server.control import control_store as store
    ctl = (registry or {}).get(device_id)
    chans = _channels(getattr(ctl, "traits_cfg", None) if ctl is not None else None)
    if not chans:
        return 404, {"status": "not-found", "reason": f"'{device_id}' has no setpoint channels"}
    body = body or {}
    ch = body.get("channel")
    if ch not in chans:
        return 400, {"status": "bad-request", "reason": f"channel must be one of {sorted(chans)}"}
    sel = dict(store.get_setting(conn, _key(device_id)) or {})
    if body.get("reset"):
        sel.pop(ch, None)
        store.set_setting(conn, _key(device_id), sel)
        return 200, {"status": "ok", "device_id": device_id, "channel": ch,
                     "comfort": (chans[ch] or {}).get("comfort"), "source": "default"}
    try:
        lo, hi = float(body["min"]), float(body["max"])
    except (KeyError, TypeError, ValueError):
        return 400, {"status": "bad-request", "reason": "min and max must be numbers"}
    mlo, mhi = (chans[ch] or {}).get("min"), (chans[ch] or {}).get("max")
    if not lo < hi:
        return 400, {"status": "bad-request", "reason": "min must be below max"}
    if (mlo is not None and lo < mlo) or (mhi is not None and hi > mhi):
        return 400, {"status": "bad-request", "reason": f"comfort must sit inside the manual range {mlo}–{mhi}"}
    sel[ch] = {"min": lo, "max": hi}
    store.set_setting(conn, _key(device_id), sel)
    return 200, {"status": "ok", "device_id": device_id, "channel": ch, "comfort": sel[ch], "source": "selected"}
