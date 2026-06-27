"""Shared construction of the CommandIssuer — used by BOTH the API control mount and the ha-controller,
so the security-critical wiring (registry + secrets + policy + transport routing) lives in ONE place
and can't drift between the two entry points.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from server.control.issuer import CommandIssuer, MqttTransport, RoutingTransport
from server.control.levoit_driver import LevoitMqttTransport, load_levoit_devices
from server.control.midea_driver import MideaTransport, load_drivers_from_env
from server.control.policy import PolicyStore
from server.control.registry import (check_secrets_present, load_control_registry, load_secrets,
                                      secrets_from_lut)
from server.control.secret_store import load_lut

log = logging.getLogger("ha.control.bootstrap")


def parse_env_file(path: Path) -> dict:
    """Parse a KEY=value env file (instance/*.env) into a dict. Missing file -> {}."""
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def build_issuer(master: str, *, control_registry: Path, node_secrets_lut: Path, control_policy: Path,
                 control_secrets: Path, midea_device_env: Path, broker: str = "localhost",
                 port: int = 1883, midea_device_id: str = "dehumidifier_office",
                 levoit_registry: Path | None = None):
    """Return (issuer, registry, midea_drivers). Secrets = per-NODE cmd_secret (encrypted LUT) +
    per-device secrets (control_secrets.yaml, for local-driver appliances). Transport routes each
    device to its backend: Midea LAN appliances -> MideaTransport, BLE nodes -> MqttTransport."""
    registry = load_control_registry(control_registry)
    lut = load_lut(node_secrets_lut, master)
    secrets = {**secrets_from_lut(registry, lut), **load_secrets(control_secrets)}
    policy_data = yaml.safe_load(control_policy.read_text()) if control_policy.exists() else None
    policy = PolicyStore(policy_data or {"version": 1})
    midea_drivers = load_drivers_from_env(parse_env_file(midea_device_env), midea_device_id)
    default_tr = MqttTransport(broker=broker, port=port)
    overrides: dict = {}
    if midea_drivers:
        mt = MideaTransport(midea_drivers)
        overrides.update({d: mt for d in midea_drivers})
    # local-ESPHome purifiers (Levoit): plain-MQTT local drivers, routed like Midea by device_id.
    levoit_registry = levoit_registry or (control_registry.parent / "levoit-devices.yaml")
    levoit_devices = load_levoit_devices(levoit_registry)
    if levoit_devices:
        lt = LevoitMqttTransport(levoit_devices, broker=broker, port=port)
        overrides.update({d: lt for d in levoit_devices})
    transport = RoutingTransport(default_tr, overrides) if overrides else default_tr
    issuer = CommandIssuer(registry=registry, secrets=secrets, policy=policy, transport=transport)
    # Loudly flag declared-but-uncommandable devices at BOOT, so a missing secret surfaces here and not
    # silently as "unknown-device" the first time something tries to actuate (the 2026-06-24 cutover
    # incident: control_secrets.yaml was never copied, so the Midea couldn't be driven). cluster-doctor
    # asserts the file is present on-box; this asserts the loaded config can actually command each device.
    missing = check_secrets_present(registry, secrets)
    if missing:
        log.warning("CONTROL: %d/%d declared device(s) have NO command secret and CANNOT be actuated "
                    "(unknown-device): %s — check instance/control_secrets.yaml / node enrollment",
                    len(missing), len(registry), ", ".join(sorted(missing)))
    return issuer, registry, midea_drivers
