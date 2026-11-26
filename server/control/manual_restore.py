"""Manual devices come back the way they were (Hugh 2026-11-26).

`purifier_c_bed` was on Manual (automation policy disabled) when a brownout turned it off on 11-19 22:42 PST; it
came back powered but OFF, and nothing put it back — it sat off for a week. Now, while a device is Manual, the
controller remembers its setting as it observes it (so a person's change — PWA or the device's own buttons — IS the
new setting), and when the device RETURNS from being away (offline, or a self-entered start-up state like the ERV's
power-on) in a different state, it puts the remembered one back.

"Away" per transport: the purifiers' ESPHome LWT (`meta.online` false, via the Levoit bridge), stale telemetry
(> AWAY_S), a level-mode device in an `external` state (the ERV's power-on start-up / OVR), and a local driver
whose status() fails (the Midea). A controller start also counts as a return — the brownout may have taken ha-2 too.

Pure decision here; the controller observes and commands (controller._tick_manual). Memory lives in control.db
`house_setting` `manual:last:<device_id>` = {running, level, mode, ts}.
"""
from __future__ import annotations

SETTLE_S = 60          # a device just back needs its own start-up first (the ERV: ~2 min in its start-up state)
RETRY_S = 60
MAX_TRIES = 3
GIVE_UP_S = 900        # still different 15 min after returning: stop, and take what it is as the new setting
AWAY_S = 600           # telemetry older than this = away (the purifier bridge re-emits every few minutes)


def key(device_id: str) -> str:
    return f"manual:last:{device_id}"


def same(mem: dict | None, obs: dict | None) -> bool:
    if mem is None or obs is None:
        return mem is obs
    if bool(mem.get("running")) != bool(obs.get("running")):
        return False
    if not obs.get("running"):
        return True                                    # both off: level/mode don't matter
    for k in ("level", "mode"):
        if mem.get(k) is not None and obs.get(k) is not None and mem[k] != obs[k]:
            return False
    return True


def decide(mem: dict | None, obs: dict | None, returned_at: float | None, tries: int, last_try: float,
           now: float) -> tuple[str, str]:
    """(action, why). action: none | remember | wait | restore | done | give_up.
    `returned_at` = when the device came back (None = it has been here all along)."""
    if obs is None:
        return "wait", "state unknown"
    if returned_at is None:
        return ("none", "") if same(mem, obs) else ("remember", "manual setting observed")
    if mem is None or same(mem, obs):
        return "done", "back as it was"
    if now - returned_at < SETTLE_S:
        return "wait", "just back: letting it start up"
    if now - returned_at > GIVE_UP_S or tries >= MAX_TRIES:
        return "give_up", f"still {describe(obs)} after {tries} tries: keeping that as the setting"
    if now - last_try < RETRY_S:
        return "wait", "restore sent: waiting for it to take"
    return "restore", f"back after being away as {describe(obs)} -> restoring {describe(mem)}"


def describe(s: dict | None) -> str:
    if not s:
        return "unknown"
    if not s.get("running"):
        return "off"
    if s.get("level") is not None:
        return f"on at level {s['level']}"
    if s.get("mode") is not None:
        return f"on in mode {s['mode']}"
    return "on"
