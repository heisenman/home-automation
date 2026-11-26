"""Manual holds on law-driven devices (docs/design/unified-control-law.md, L4; Hugh 2026-11-26).

A person who sets a device the law drives — from the PWA, or (thermostat) at the wall — gets their setting kept:
the law yields that device for `hold_h` hours or until the next goal-schedule change, whichever is sooner, then
takes it back. The PWA shows each hold with a "resume law" button (DELETE /control/house/law/hold/{device}).

Stored in control.db `house_setting` `law:hold:<device_id>` = {by: "pwa"|"wall", ts, until, detail}, so a hold
survives a controller restart and rides the standby snapshot like every other house setting.
"""
from __future__ import annotations

from server.control.law import bands as B

PREFIX = "law:hold:"


def _key(device_id: str) -> str:
    return PREFIX + device_id


def expiry(conn, store, now: float, settings: dict | None = None) -> float:
    st = settings or B.get(conn, store)
    until = now + float(st["bands"].get("hold_h") or 2.0) * 3600
    nxt = B.next_change(st["bands"], now, store.get_setting(conn, "timezone"))
    return min(until, nxt) if nxt else until


def place(conn, store, device_id: str, by: str, now: float, detail: dict | None = None) -> dict:
    h = {"by": by, "ts": now, "until": expiry(conn, store, now), "detail": detail or {}}
    store.set_setting(conn, _key(device_id), h)
    return h


def active(conn, store, device_id: str, now: float) -> dict | None:
    h = store.get_setting(conn, _key(device_id))
    if not h:
        return None
    if now >= float(h.get("until") or 0):
        store.set_setting(conn, _key(device_id), None)      # expired: the law takes the device back
        return None
    return h


def clear(conn, store, device_id: str) -> bool:
    had = bool(store.get_setting(conn, _key(device_id)))
    store.set_setting(conn, _key(device_id), None)
    return had


def all_active(conn, now: float) -> dict:
    out = {}
    import json
    for key, raw in conn.execute("SELECT key, json FROM house_setting WHERE key LIKE ?", (PREFIX + "%",)):
        try:
            h = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if h and now < float(h.get("until") or 0):
            out[key[len(PREFIX):]] = h
    return out


def on_manual_command(conn, store, device_id: str, body: dict, now: float) -> dict | None:
    """A person's command reached a device from the PWA (POST /devices/{id}/command, 200). If the law drives that
    device, hold it. Returns the hold placed, or None (not law-driven, or a command that doesn't set a state)."""
    st = B.get(conn, store)
    if B.enrolment(st, device_id) != "law":
        return None
    if (body or {}).get("trait") not in ("switchable", "ranged", "mode", "setpoint", "timed"):
        return None
    return place(conn, store, device_id, "pwa", now,
                 {"trait": body.get("trait"), "args": body.get("args") or {}})


def describe(h: dict, tz: str | None = None) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from server.control.automation import HOUSE_TZ_DEFAULT, valid_tz
    t = datetime.fromtimestamp(float(h["until"]), ZoneInfo(tz if valid_tz(tz) else HOUSE_TZ_DEFAULT))
    who = "set at the thermostat" if h.get("by") == "wall" else "set from the PWA"
    return f"manual hold ({who}) until {t:%H:%M}"
