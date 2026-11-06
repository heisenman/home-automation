"""Per-plug power mode — may this plug be switched OFF from software? (Hugh, 2026-11-05; pure, plus store I/O.)

    on_off      On, Off and Power-cycle (the appliance plugs: Midea, Aprilaire, ERV — and the air-gap router,
                by Hugh's choice, though an Off there cuts HA's path to the plug: only its button restores it)
    cycle_only  On and Power-cycle; Off is refused. For plugs feeding computers (the failover box, the G11):
                the plug-side cycle always restores power, a plain Off may strand the box.

The default comes from the registry (traits.switchable.power_mode); the selected mode is a house setting in
control.db (`power_mode:<device_id>`), so it rides the standby snapshot and is set from the PWA
(PUT /control/<id>/power-mode). Enforced in the API before the issuer — the controller never commands plugs.
"""
from __future__ import annotations

MODES = ("on_off", "cycle_only")
MODE_LABELS = {"on_off": "On / off", "cycle_only": "Power-cycle only"}


def _switch_cfg(registry, device_id) -> dict | None:
    ctl = (registry or {}).get(device_id)
    tc = (getattr(ctl, "traits_cfg", None) or {}) if ctl is not None else {}
    return tc.get("switchable") if isinstance(tc.get("switchable"), dict) else None


def has_mode(registry, device_id) -> bool:
    """Only devices whose switchable trait declares a power_mode (the smart plugs) carry one."""
    sw = _switch_cfg(registry, device_id)
    return bool(sw and sw.get("power_mode") in MODES)


def default_mode(registry, device_id) -> str | None:
    sw = _switch_cfg(registry, device_id)
    return sw.get("power_mode") if sw and sw.get("power_mode") in MODES else None


def get_mode(conn, registry, device_id) -> str | None:
    from server.control import control_store as store
    d = default_mode(registry, device_id)
    if d is None:
        return None
    v = store.get_setting(conn, f"power_mode:{device_id}") if conn is not None else None
    return v if v in MODES else d


def refusal(mode: str | None, trait: str, action: str, args: dict) -> str | None:
    """Why this command is refused under `mode`, or None if allowed."""
    if mode == "cycle_only" and trait == "switchable" and action == "set" and args.get("on") in (False, 0, "off",
                                                                                                  "false"):
        return ("this plug is set to power-cycle only: switching it off could strand the computer it feeds. "
                "Use Power-cycle, or change its power mode first.")
    return None


def handle_set_mode(conn, registry, device_id: str, body: dict) -> tuple[int, dict]:
    from server.control import control_store as store
    if not has_mode(registry, device_id):
        return 404, {"status": "not-found", "reason": f"'{device_id}' has no power mode (not a smart plug)"}
    mode = (body or {}).get("mode")
    if mode not in MODES:
        return 400, {"status": "bad-request", "reason": f"mode must be one of {list(MODES)}"}
    store.set_setting(conn, f"power_mode:{device_id}", mode)
    return 200, {"status": "ok", "device_id": device_id, "power_mode": mode}
