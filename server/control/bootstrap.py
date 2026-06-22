"""Shared construction of the CommandIssuer — used by BOTH the API control mount and the ha-controller,
so the security-critical wiring (registry + secrets + policy + transport routing) lives in ONE place
and can't drift between the two entry points.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from server.control.issuer import CommandIssuer, MqttTransport, RoutingTransport
from server.control.midea_driver import MideaTransport, load_drivers_from_env
from server.control.policy import PolicyStore
from server.control.registry import load_control_registry, load_secrets, secrets_from_lut
from server.control.secret_store import load_lut


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
                 port: int = 1883, midea_device_id: str = "dehumidifier_office"):
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
    if midea_drivers:
        mt = MideaTransport(midea_drivers)
        transport = RoutingTransport(default_tr, {d: mt for d in midea_drivers})
    else:
        transport = default_tr
    issuer = CommandIssuer(registry=registry, secrets=secrets, policy=policy, transport=transport)
    return issuer, registry, midea_drivers
