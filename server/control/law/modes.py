"""Who runs each device — the PWA's three-way switch (Hugh 2026-11-26): Manual / Own automation / Law.

It's a view over two settings that already exist, so nothing else in the controller changes meaning:
  manual  the device's automation policy `enabled: false` — the controller doesn't decide for it; a person does.
          While Manual, its setting is remembered and put back after a power loss (server/control/manual_restore.py).
  auto    policy enabled, enrolment `shadow` — its own rule runs; the law is compared alongside it
  law     policy enabled, enrolment `law` — the law drives it (controller._apply_law)
A device with no automation policy (the thermostat) has no rule of its own: manual/auto both mean "the law
leaves it alone" (enrolment shadow); law holds it at the temperature band.
"""
from __future__ import annotations

from server.control.law import bands as B

MODES = ("manual", "auto", "law")


def of(conn, store, device_id: str, settings: dict | None = None) -> str:
    st = settings or B.get(conn, store)
    pol = store.get_policy(conn, device_id)
    if pol is not None and not pol.get("enabled", True):
        return "manual"
    if B.enrolment(st, device_id) == "law":
        return "law"
    return "auto" if pol is not None else "manual"


def apply(conn, store, modes: dict, devices=None) -> tuple[int, dict]:
    """PUT body `modes` {device_id: manual|auto|law}. Validates all first, then writes policy + enrolment."""
    if not isinstance(modes, dict):
        return 400, {"status": "bad-request", "reason": "modes must be {device_id: manual|auto|law}"}
    for d, m in modes.items():
        if m not in MODES:
            return 400, {"status": "bad-request", "reason": f"modes[{d}] must be one of {list(MODES)}"}
        if devices is not None and d not in devices:
            return 400, {"status": "bad-request", "reason": f"modes names an unknown device: {d}"}
    st = B.get(conn, store)
    enrol = dict(st["enrol"])
    for d, m in modes.items():
        pol = store.get_policy(conn, d)
        if pol is not None:
            want = m != "manual"
            if bool(pol.get("enabled", True)) != want:
                store.set_policy(conn, d, {**pol, "enabled": want})
        if m == "law":
            enrol[d] = "law"
        elif enrol.get(d) == "law":
            enrol[d] = "shadow"
    store.set_setting(conn, B.KEYS["enrol"], enrol)
    return 200, {"status": "ok", "modes": {d: of(conn, store, d) for d in modes}}
