"""Capability contract — the trait vocabulary (ADR-0002, plan §11.1).

Devices are described by a small set of traits, not product names: `switchable`, `ranged`,
`positionable`, `lockable`, `setpoint`. Policies and commands target traits, so new hardware of a
known shape is inducted with no new admin code. A bespoke driver may live *below* this interface,
but the interface itself never leaks product specifics upward.

This module is the single source of truth for:
  - what state shape each trait reports,
  - what actions each trait accepts and how their args are validated/normalised,
  - which actions are *sensitive* (require a freshness nonce + confirm — e.g. unlock),
  - each trait's fail-safe state (used by standing orders / disconnected fallback).

Pure functions, no I/O — unit-testable in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class TraitError(ValueError):
    """A command/state failed validation against the capability contract."""


# An action validator takes (raw_args, trait_config) and returns the normalised args dict,
# or raises TraitError. trait_config is the per-device per-trait config from the registry
# (e.g. {"min": 10, "max": 30, "unit": "degC"} for a setpoint).
Validator = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class Trait:
    name: str
    state_keys: tuple[str, ...]                 # keys a reported/intended state must carry
    actions: dict[str, Validator]               # action name -> arg validator
    sensitive_actions: frozenset[str]           # actions needing nonce + confirm
    safe_state: Callable[[dict[str, Any]], dict[str, Any]]  # cfg -> fail-safe state

    def validate_command(self, action: str, args: dict[str, Any],
                         cfg: dict[str, Any]) -> dict[str, Any]:
        if action not in self.actions:
            raise TraitError(f"trait '{self.name}' has no action '{action}' "
                             f"(valid: {sorted(self.actions)})")
        return self.actions[action](args or {}, cfg or {})

    def is_sensitive(self, action: str) -> bool:
        return action in self.sensitive_actions


# ── helpers ──────────────────────────────────────────────────────────────────────
def _as_bool(v: Any, field_name: str) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int,)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.lower() in ("true", "false", "on", "off", "1", "0"):
        return v.lower() in ("true", "on", "1")
    raise TraitError(f"'{field_name}' must be a boolean, got {v!r}")


def _as_number_in_range(v: Any, field_name: str, lo: float, hi: float,
                        integer: bool) -> float | int:
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise TraitError(f"'{field_name}' must be a number, got {v!r}")
    if integer and n != int(n):
        raise TraitError(f"'{field_name}' must be an integer, got {v!r}")
    if not (lo <= n <= hi):
        raise TraitError(f"'{field_name}'={n} out of range [{lo}, {hi}]")
    return int(n) if integer else n


# ── trait definitions ─────────────────────────────────────────────────────────────
def _switchable_set(args, cfg):
    return {"on": _as_bool(args.get("on"), "on")}


def _ranged_set(args, cfg):
    lo, hi = cfg.get("min", 0), cfg.get("max", 100)
    return {"level": _as_number_in_range(args.get("level"), "level", lo, hi, integer=True)}


def _positionable_set(args, cfg):
    lo, hi = cfg.get("min", 0), cfg.get("max", 100)
    return {"position": _as_number_in_range(args.get("position"), "position", lo, hi, integer=True)}


def _setpoint_set(args, cfg):
    lo, hi = cfg.get("min", float("-inf")), cfg.get("max", float("inf"))
    val = _as_number_in_range(args.get("value"), "value", lo, hi, integer=False)
    return {"value": val}


_TRAITS: dict[str, Trait] = {
    "switchable": Trait(
        name="switchable", state_keys=("on",),
        actions={"set": _switchable_set},
        sensitive_actions=frozenset(),
        safe_state=lambda cfg: {"on": bool(cfg.get("safe_on", False))},
    ),
    "ranged": Trait(
        name="ranged", state_keys=("level",),
        actions={"set": _ranged_set},
        sensitive_actions=frozenset(),
        safe_state=lambda cfg: {"level": int(cfg.get("safe_level", cfg.get("min", 0)))},
    ),
    "positionable": Trait(
        name="positionable", state_keys=("position",),
        actions={"set": _positionable_set},
        sensitive_actions=frozenset(),
        safe_state=lambda cfg: {"position": int(cfg.get("safe_position", cfg.get("min", 0)))},
    ),
    "lockable": Trait(
        name="lockable", state_keys=("locked",),
        actions={
            "lock": lambda args, cfg: {"locked": True},
            "unlock": lambda args, cfg: {"locked": False},
        },
        sensitive_actions=frozenset({"unlock"}),   # unlock is the canonical sensitive action
        safe_state=lambda cfg: {"locked": True},     # fail-safe is LOCKED
    ),
    "setpoint": Trait(
        name="setpoint", state_keys=("value",),
        actions={"set": _setpoint_set},
        sensitive_actions=frozenset(),
        safe_state=lambda cfg: {"value": cfg["safe_value"]} if "safe_value" in cfg else {},
    ),
}


def get_trait(name: str) -> Trait:
    t = _TRAITS.get(name)
    if t is None:
        raise TraitError(f"unknown trait '{name}' (known: {sorted(_TRAITS)})")
    return t


def known_traits() -> list[str]:
    return sorted(_TRAITS)


def validate_device_traits(traits: list[str]) -> None:
    """Raise if a device declares an unknown trait (used at registry load / onboarding)."""
    for t in traits:
        get_trait(t)
