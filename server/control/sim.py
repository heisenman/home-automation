"""Simulated actuator — a stand-in device that enforces the command handshake.

Lets the whole control plane be exercised end-to-end with NO hardware: it holds a per-device secret,
verifies every command's signature/nonce/freshness (protocol.Verifier), validates the action against
its declared traits (capability contract), applies it to in-memory state, and returns an ack with the
reported state. Forged / tampered / stale / replayed / out-of-contract commands are refused.

This is the node-side enforcement the real firmware will mirror (HMAC verify + trait apply + ack).
"""
from __future__ import annotations

from typing import Any

from . import protocol, traits


class SimActuator:
    def __init__(self, device_id: str, traits_cfg: dict[str, dict[str, Any]],
                 secret: str, max_age_s: float = protocol.DEFAULT_MAX_AGE_S):
        traits.validate_device_traits(list(traits_cfg))
        self.device_id = device_id
        self.traits_cfg = traits_cfg
        self.verifier = protocol.Verifier(secret, max_age_s=max_age_s)
        self.state: dict[str, Any] = {}
        for tname, cfg in traits_cfg.items():               # boot to fail-safe state
            self.state.update(traits.get_trait(tname).safe_state(cfg))

    def handle_command(self, cmd: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        """Return an ack dict. source='commanded' on success; status='rejected' with a reason otherwise."""
        cmd_id = cmd.get("id", "?")
        ok, reason = self.verifier.verify(cmd, now=now)
        if not ok:
            return protocol.build_ack(cmd_id=cmd_id, status="rejected", reason=reason,
                                      reported_state=self._report(), ts=now)
        tname = cmd.get("trait")
        if tname not in self.traits_cfg:
            return protocol.build_ack(cmd_id=cmd_id, status="rejected", reason="no-such-trait",
                                      reported_state=self._report(), ts=now)
        trait = traits.get_trait(tname)
        try:
            norm = trait.validate_command(cmd.get("action", ""), cmd.get("args", {}),
                                          self.traits_cfg[tname])
        except traits.TraitError as e:
            return protocol.build_ack(cmd_id=cmd_id, status="rejected", reason=str(e),
                                      reported_state=self._report(), ts=now)
        self.state.update(norm)                              # apply
        return protocol.build_ack(cmd_id=cmd_id, status="ok", reported_state=self._report(),
                                  source="commanded", ts=now)

    def _report(self) -> dict[str, Any]:
        """Current reported state across all declared traits."""
        keys: list[str] = []
        for tname in self.traits_cfg:
            keys.extend(traits.get_trait(tname).state_keys)
        return {k: self.state[k] for k in keys if k in self.state}
