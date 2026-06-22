"""Control registry + per-device secret loading (config-driven, modular).

Actuators are declared in `instance/control.yaml` (gitignored) and their per-device HMAC secrets in
`instance/control_secrets.yaml` (gitignored, console-enrolled). Keeping these as plain files — separate
from the sensor `devices.yaml` — keeps the control plane modular and **failover-ready**: the standby
dictator gets the same authority by syncing these files (no code, no central DB coupling).

Parsing is separated from file I/O so it is unit-testable with in-memory dicts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import traits
from .issuer import DeviceCtl


def parse_control_registry(data: dict[str, Any]) -> dict[str, DeviceCtl]:
    """{devices: {id: {node, area, traits: {trait: cfg}}}} -> {id: DeviceCtl}. Validates traits."""
    out: dict[str, DeviceCtl] = {}
    for dev_id, spec in (data.get("devices") or {}).items():
        tcfg = spec.get("traits") or {}
        traits.validate_device_traits(list(tcfg))
        if not spec.get("node") or not spec.get("area"):
            raise ValueError(f"control device '{dev_id}' missing node/area")
        out[dev_id] = DeviceCtl(device_id=dev_id, node=spec["node"], area=spec["area"],
                                traits_cfg={t: (c or {}) for t, c in tcfg.items()})
    return out


def load_control_registry(path: Path) -> dict[str, DeviceCtl]:
    if not path.exists():
        return {}
    with path.open() as f:
        return parse_control_registry(yaml.safe_load(f) or {})


def load_secrets(path: Path) -> dict[str, str]:
    """{device_id: secret}. Missing file → empty (no devices controllable until enrolled)."""
    if not path.exists():
        return {}
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    return {str(k): str(v) for k, v in data.items()}


def check_secrets_present(registry: dict[str, DeviceCtl], secrets: dict[str, str]) -> list[str]:
    """Return device_ids that are declared but have no secret (cannot be commanded). For startup warnings."""
    return [d for d in registry if d not in secrets]


def secrets_from_lut(registry: dict[str, DeviceCtl], lut: dict[str, dict]) -> dict[str, str]:
    """Map each control device to its owning NODE's `cmd_secret` from the encrypted enrollment LUT.

    The HMAC command key is per-NODE (baked into that node's firmware and verified by the node, which may
    relay to several end devices), so every device on `node` signs with `lut[node]["cmd_secret"]`. Devices
    whose node isn't enrolled (or whose record lacks a secret) are omitted — they simply can't be commanded
    yet (see check_secrets_present for the startup warning). This is the bridge that lets the PEP source
    secrets from the encrypted store instead of an inline dict (control go-live, 2026-06-21)."""
    out: dict[str, str] = {}
    for dev_id, ctl in registry.items():
        rec = lut.get(ctl.node)
        if rec and rec.get("cmd_secret"):
            out[dev_id] = rec["cmd_secret"]
    return out
