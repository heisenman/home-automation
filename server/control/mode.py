"""Whole-house operating mode (plan §11.6).

A first-class, server-owned global mode (Normal / Conserve / Emergency) that multiplies every device's
policy (see policy.PolicyStore.evaluate / standing_orders). Composes with server-down fallback: a node
with low power + no server runs its pre-provisioned conservation standing orders autonomously.

Requirements honoured:
  - **Server-declared + authenticated.** A spoofed "enter Conserve" is a DoS/mischief vector, so a
    manual set must be authorised by the caller (the API behind admin auth).
  - **Hysteresis.** Auto transitions use a deadband on the trigger signals AND a minimum dwell time,
    so the mode cannot thrash near a threshold.
  - **Pluggable inputs.** The auto-driver consumes a signals dict (mains_present, ups_pct, …). The real
    sources (mains-present sensor, UPS state, whole-house power monitor) are a TBD hardware dependency;
    the mechanism is built now and works the moment signals are wired in.

`now` is injected so transitions are deterministic and testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MODES = ("Normal", "Conserve", "Emergency")


@dataclass
class ModeThresholds:
    # deadband: enter at the strict bound, leave only past the relaxed bound (prevents flapping)
    conserve_enter_ups: float = 40.0       # enter Conserve when ups_pct <= this (or mains lost)
    conserve_leave_ups: float = 60.0       # leave Conserve only when ups_pct >= this AND mains present
    emergency_enter_ups: float = 15.0      # enter Emergency when ups_pct <= this
    emergency_leave_ups: float = 25.0      # de-escalate Emergency only when ups_pct >= this


@dataclass
class ModeController:
    mode: str = "Normal"
    hysteresis_s: float = 300.0            # minimum dwell before an AUTO transition may switch again
    thresholds: ModeThresholds = field(default_factory=ModeThresholds)
    _since: float | None = None            # ts of last transition

    def get(self) -> str:
        return self.mode

    def set(self, mode: str, *, authorized: bool, now: float | None = None) -> bool:
        """Manual, authenticated override. Returns False if unauthorised; raises on a bad mode."""
        if not authorized:
            return False
        if mode not in MODES:
            raise ValueError(f"unknown mode '{mode}'")
        if mode != self.mode:
            self.mode = mode
            self._since = now
        return True

    def _desired(self, signals: dict[str, Any]) -> str:
        """Map signals → desired mode using deadband relative to the CURRENT mode."""
        mains = bool(signals.get("mains_present", True))
        ups = float(signals.get("ups_pct", 100.0))
        t = self.thresholds
        cur = self.mode
        # Escalation is immediate (safety); de-escalation uses the relaxed (leave) bounds.
        if ups <= t.emergency_enter_ups:
            return "Emergency"
        if cur == "Emergency":
            return "Emergency" if ups < t.emergency_leave_ups else "Conserve"
        if (not mains) or ups <= t.conserve_enter_ups:
            return "Conserve"
        if cur == "Conserve":
            return "Conserve" if (ups < t.conserve_leave_ups or not mains) else "Normal"
        return "Normal"

    def tick(self, signals: dict[str, Any], now: float) -> str:
        """Auto-evaluate from signals with dwell hysteresis. Returns the (possibly new) mode."""
        desired = self._desired(signals)
        if desired == self.mode:
            return self.mode
        # escalations bypass dwell (safety); only de-escalations wait out the dwell window
        escalating = MODES.index(desired) > MODES.index(self.mode)
        if not escalating and self._since is not None and (now - self._since) < self.hysteresis_s:
            return self.mode
        self.mode = desired
        self._since = now
        return self.mode
