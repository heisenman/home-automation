"""Aprilaire E070 issuer Transport — a LEASED `DH` call through edge node dehum_c6 (ADR-0041, ADR-0010).

The E070 in External mode treats `DH` as a request: closed = dehumidify. The node only ever closes it as a
lease (`dehum_call {min}`), so the controller RENEWS while it still wants dehumidification and a lost server
lets the call run out on its own. Confirmation is the node's pin readback (`dh_call` on
home/edge/<node>/dehum/adv); whether the unit actually RUNS is a separate question answered by its metering
plug (see viewmodel verify_power) — the E070 has no status output.

    switchable {on: true}  -> {"op":"dehum_call","min": <lease_min>}   reported {"on": dh_call}
    switchable {on: false} -> {"op":"dehum_call","min": 0}
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import protocol
from .edge_cmd import STATE_DIR
from .erv_driver import CONFIRM_S, send_and_confirm

log = logging.getLogger("ha.control.dehum")

DEFAULT_LEASE_MIN = 10


def dehum_devices_of(registry: dict) -> dict[str, tuple[str, int]]:
    """{device_id: (node, lease_min)} for every control device of type `dehum` (ADR-0026: by TYPE)."""
    out = {}
    for d, c in registry.items():
        if getattr(c, "device_type", None) == "dehum":
            lease = int(((getattr(c, "traits_cfg", {}) or {}).get("switchable") or {}).get("lease_min",
                                                                                             DEFAULT_LEASE_MIN))
            out[d] = (c.node, max(1, min(60, lease)))
    return out


class DehumEdgeTransport:
    def __init__(self, devices: dict[str, tuple[str, int]], lut: dict, broker: str = "localhost",
                 port: int = 1883, state_dir: Path = STATE_DIR, confirm_s: float = CONFIRM_S):
        import paho.mqtt.client as mqtt
        self._mqtt = mqtt
        self.devices = devices
        self.lut = lut
        self.broker, self.port = broker, port
        self.state_dir = state_dir
        self.confirm_s = confirm_s

    def send_and_wait(self, *, node, device_id, area, cmd, now=None, timeout=5.0):
        if device_id not in self.devices:
            return None
        node, lease = self.devices[device_id]
        trait, action, args = cmd.get("trait"), cmd.get("action"), cmd.get("args", {})
        if trait != "switchable" or action != "set":
            return protocol.build_ack(cmd_id=cmd["id"], status="rejected",
                                      reason=f"dehum: unsupported {trait}/{action}")
        on = bool(args.get("on"))
        secret = (self.lut.get(node) or {}).get("cmd_secret")
        if not secret:
            return protocol.build_ack(cmd_id=cmd["id"], status="rejected",
                                      reason=f"dehum: no cmd_secret for node {node} in the LUT")
        matches = (lambda m: bool(m.get("dh_call")) == on)
        seen = send_and_confirm(self._mqtt, self.broker, self.port, node, secret,
                                {"op": "dehum_call", "min": lease if on else 0},
                                f"home/edge/{node}/dehum/adv", matches, max(timeout, self.confirm_s),
                                self.state_dir, device_id)
        if not seen:
            return None
        hit = next((m for m in reversed(seen) if matches(m)), seen[-1])
        return protocol.build_ack(cmd_id=cmd["id"], status="ok",
                                  reported_state={"on": bool(hit.get("dh_call"))}, source="commanded")
