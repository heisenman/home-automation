"""Pure automation-policy resolver (ADR-0011) — no I/O, fully unit-tested.

This is the CONTROL LAW: given a device's policy + the current sensor reading + device state + any
active manual override, resolve the desired actuator command. It is distinct from policy.py, which is
the AUTHORIZATION gate (guardrails/modes/confirm — *whether* a command is allowed). The controller
runs this resolver to decide WHAT the device should do, then sends that desired command through
PolicyStore.evaluate + the issuer (so automation rides the same signed/ACL path as a human command).

Precedence stack (highest wins):
    1. SAFETY / interlocks   tank_full|error -> force OFF (immediate)
    2. MANUAL override (TTL)  off | boost_on, expiring
    3. SCHEDULE              an "off" time-window (passed in precomputed as schedule_off)
    4. CONTROL rule          pluggable strategy (hysteresis | setpoint)
    5. DEFAULT               safe resting state

Compressor cycle gating is then applied to any transition:
    - turning ON  -> blocked until min_off elapsed since last off (restart protection; ALL layers)
    - turning OFF -> blocked until min_on elapsed since last on (anti-chatter; rule/schedule/default
                     only — safety and manual override turn off immediately)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reading:
    value: float
    ts: float            # epoch seconds of the sensor sample


@dataclass(frozen=True)
class DeviceState:
    running: bool
    interlocks: tuple = ()          # active interlock names, e.g. ("tank_full",)
    last_on_ts: float | None = None
    last_off_ts: float | None = None


@dataclass(frozen=True)
class Override:
    action: str                     # "off" | "boost_on" | "clear"
    expiry: float | None = None     # epoch; None = until cleared

    def active(self, now: float) -> bool:
        return self.action in ("off", "boost_on") and (self.expiry is None or self.expiry > now)


@dataclass(frozen=True)
class Policy:
    strategy: str = "hysteresis"    # "hysteresis" (external/own sensor) | "setpoint" (trust device)
    on_above: float = 55.0
    off_below: float = 50.0
    min_on_s: float = 600.0
    min_off_s: float = 300.0
    default_running: bool = False
    sensor_stale_s: float = 600.0

    @classmethod
    def from_dict(cls, d: dict) -> "Policy":
        d = d or {}
        c = d.get("control", {}) or {}
        return cls(
            strategy=c.get("strategy", "hysteresis"),
            on_above=float(c.get("on_above", 55)),
            off_below=float(c.get("off_below", 50)),
            min_on_s=float(c.get("min_on_min", 10)) * 60.0,
            min_off_s=float(c.get("min_off_min", 5)) * 60.0,
            default_running=bool((d.get("defaults", {}) or {}).get("running", False)),
            sensor_stale_s=float(d.get("sensor_stale_min", 10)) * 60.0,
        )


@dataclass(frozen=True)
class Resolution:
    running: bool          # the state we will command or hold
    act: bool              # True = issue a command (running differs from current and is allowed)
    source: str            # which layer decided: safety|override|schedule|rule|default
    reason: str


_IMMEDIATE_OFF = ("safety", "override")   # layers allowed to turn off bypassing min-on


def _exp(label: str, ov: Override, now: float) -> str:
    if ov.expiry is None:
        return f"{label} (until cleared)"
    return f"{label} ({(ov.expiry - now) / 60:.0f}m left)"


def _gate(policy: Policy, now: float, state: DeviceState, want: bool, source: str, reason: str) -> Resolution:
    """Apply compressor cycle protection to a desired transition."""
    cur = state.running
    if want == cur:
        return Resolution(cur, False, source, reason + " (no-op)")
    if want is False:                                   # turning OFF
        if source in _IMMEDIATE_OFF:
            return Resolution(False, True, source, reason)
        if state.last_on_ts is not None and (now - state.last_on_ts) < policy.min_on_s:
            left = (policy.min_on_s - (now - state.last_on_ts)) / 60
            return Resolution(True, False, source, f"{reason}; held ON (min-on {left:.0f}m left)")
        return Resolution(False, True, source, reason)
    # turning ON — min-off restart protection applies to every layer
    if state.last_off_ts is not None and (now - state.last_off_ts) < policy.min_off_s:
        left = (policy.min_off_s - (now - state.last_off_ts)) / 60
        return Resolution(False, False, source, f"{reason}; held OFF (min-off {left:.0f}m left)")
    return Resolution(True, True, source, reason)


def resolve(policy: Policy, now: float, sensor: Reading | None, state: DeviceState,
            override: Override | None = None, schedule_off: bool = False) -> Resolution:
    """Resolve the desired actuator state for one device at time `now`."""
    # 1. safety / interlocks
    if state.interlocks:
        return _gate(policy, now, state, False, "safety",
                     f"interlock {','.join(state.interlocks)} -> OFF")
    # 2. manual override
    if override is not None and override.active(now):
        if override.action == "off":
            return _gate(policy, now, state, False, "override", _exp("override OFF", override, now))
        if override.action == "boost_on":
            return _gate(policy, now, state, True, "override", _exp("override BOOST-ON", override, now))
    # 3. schedule
    if schedule_off:
        return _gate(policy, now, state, False, "schedule", "schedule window -> OFF")
    # 4. control rule
    if policy.strategy == "setpoint":
        # trust the device's own loop: keep it powered, it self-regulates to its target.
        return _gate(policy, now, state, True, "rule", "setpoint strategy -> device self-regulates")
    if policy.strategy == "hysteresis":
        if sensor is None or (now - sensor.ts) > policy.sensor_stale_s:
            return _gate(policy, now, state, policy.default_running, "default",
                         "sensor stale/missing -> default")
        if sensor.value >= policy.on_above:
            return _gate(policy, now, state, True, "rule",
                         f"RH {sensor.value:.0f} >= {policy.on_above:.0f} -> ON")
        if sensor.value <= policy.off_below:
            return _gate(policy, now, state, False, "rule",
                         f"RH {sensor.value:.0f} <= {policy.off_below:.0f} -> OFF")
        return Resolution(state.running, False, "rule",
                          f"RH {sensor.value:.0f} in deadband -> hold {'ON' if state.running else 'OFF'}")
    # 5. default (unknown strategy)
    return _gate(policy, now, state, policy.default_running, "default", "default")


# ── schedule window helpers (pure) ───────────────────────────────────────────────
def _parse_hhmm(s: str) -> int:
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


def in_window(tod_min: int, window: str) -> bool:
    """tod_min = minute-of-day [0,1440). window = 'HH:MM-HH:MM' (may wrap past midnight)."""
    a, b = window.split("-")
    start, end = _parse_hhmm(a), _parse_hhmm(b)
    if start <= end:
        return start <= tod_min < end
    return tod_min >= start or tod_min < end           # wraps midnight (e.g. 22:00-07:00)


def schedule_off_now(schedule: list, tod_min: int) -> bool:
    """schedule = [{"when": "HH:MM-HH:MM", "policy": "off"|"auto"}]. True iff an 'off' window matches."""
    for entry in schedule or []:
        if entry.get("policy") == "off" and in_window(tod_min, entry.get("when", "")):
            return True
    return False
