"""Policy store + Policy Enforcement decisions (plan §11.3, §11.6).

A versioned, validated policy targets *traits*, not products. Every command is evaluated here before
it is signed/issued. Categories (plan §11.3): guardrails (min/max via trait cfg, rate limits, allowed
hours, interlocks), command authorization (confirm on sensitive actions), and whole-house modes
(Normal / Conserve / Emergency) acting as a multiplier on per-device policy. Standing orders (the
disconnected fallback / fail-safe state) fall out of the same model and are what gets pushed to nodes.

Pure logic, no I/O — `now`/`last_cmd_ts` are injected so it's deterministic and testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import traits

MODES = ("Normal", "Conserve", "Emergency")


@dataclass
class Decision:
    allow: bool
    reason: str                                  # "ok" or a deny reason
    require_confirm: bool = False
    normalized_args: dict[str, Any] = field(default_factory=dict)


class PolicyError(ValueError):
    pass


class PolicyStore:
    """Holds validated policy. Shape (all optional):

        version: 1
        guardrails:
          <device_id>:
            <trait>: {rate_limit_s: 5, allowed_hours: [6, 22], require_confirm: true}
        modes:
          Emergency: {deny: [{trait: lockable, action: unlock}], force_safe: true}
          Conserve:  {deny: [{trait: ranged, action: set}]}
    """

    def __init__(self, data: dict[str, Any] | None = None):
        self.data = data or {}
        self._validate()

    def _validate(self) -> None:
        for mode in self.data.get("modes", {}):
            if mode not in MODES:
                raise PolicyError(f"unknown mode '{mode}' (valid: {MODES})")
        for dev, tmap in self.data.get("guardrails", {}).items():
            for tname in tmap:
                traits.get_trait(tname)          # raises on unknown trait

    def _guard(self, device_id: str, trait: str) -> dict[str, Any]:
        return self.data.get("guardrails", {}).get(device_id, {}).get(trait, {})

    def evaluate(self, *, device_id: str, traits_cfg: dict[str, dict], trait: str, action: str,
                 args: dict[str, Any], mode: str = "Normal", confirmed: bool = False,
                 now: float | None = None, last_cmd_ts: float | None = None) -> Decision:
        if mode not in MODES:
            return Decision(False, f"bad-mode:{mode}")
        if trait not in traits_cfg:
            return Decision(False, "device-lacks-trait")

        tr = traits.get_trait(trait)

        # 1) capability-contract validation (range/type bounds incl per-device cfg)
        try:
            norm = tr.validate_command(action, args, traits_cfg[trait])
        except traits.TraitError as e:
            return Decision(False, f"contract:{e}")

        # 2) whole-house mode multiplier (mode can forbid actions outright)
        for rule in self.data.get("modes", {}).get(mode, {}).get("deny", []):
            if rule.get("trait") == trait and rule.get("action") == action:
                return Decision(False, f"mode-{mode}-denies")

        guard = self._guard(device_id, trait)

        # 3) allowed hours (local clock; [start, end] inclusive, wraps if start > end)
        hours = guard.get("allowed_hours")
        if hours and now is not None:
            hr = datetime.fromtimestamp(now).hour
            lo, hi = hours
            inside = (lo <= hr <= hi) if lo <= hi else (hr >= lo or hr <= hi)
            if not inside:
                return Decision(False, "outside-allowed-hours")

        # 4) rate limit
        rl = guard.get("rate_limit_s")
        if rl and now is not None and last_cmd_ts is not None and (now - last_cmd_ts) < rl:
            return Decision(False, "rate-limited")

        # 5) command authorization: sensitive trait actions (or policy) need a confirm factor
        need_confirm = tr.is_sensitive(action) or bool(guard.get("require_confirm"))
        if need_confirm and not confirmed:
            return Decision(False, "confirm-required", require_confirm=True, normalized_args=norm)

        return Decision(True, "ok", require_confirm=need_confirm, normalized_args=norm)

    def standing_orders(self, *, device_id: str, traits_cfg: dict[str, dict],
                        mode: str = "Normal") -> dict[str, Any]:
        """Fail-safe state to push to a node for disconnected operation (plan §11.3 standing orders).
        Emergency with force_safe drives every trait to its safe state (e.g. all lockables LOCKED)."""
        force = self.data.get("modes", {}).get(mode, {}).get("force_safe", False)
        if mode == "Emergency":
            force = self.data.get("modes", {}).get("Emergency", {}).get("force_safe", True)
        orders: dict[str, Any] = {}
        if force:
            for tname, cfg in traits_cfg.items():
                orders.update(traits.get_trait(tname).safe_state(cfg))
        return orders
