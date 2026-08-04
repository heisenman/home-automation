"""Voluntary load-shed decision law — pure, no I/O, fully unit-tested (ADR-0037).

Given a shed window from some signal source, the current guardrail reading, and what the lane believes
it already did, decide per device: shed / release / hold. The runner does the I/O; this module holds the
judgement, so every rule below is testable without a network or a controller.

Three properties this is built around, in priority order:

1. **A human always wins.** If a device already carries an override this lane did not set, we do not
   touch it — not to shed, not to release. The precedence stack (ADR-0011) puts a manual override above
   ambient policy, and an automatic curtailment is ambient by definition. A grid event must never cancel
   somebody's deliberate pause, and must never be cancelled into one either.

2. **An unverifiable guardrail forbids the shed.** If the guardrail reading is missing or stale we cannot
   show the house is safe to curtail, so we do not shed — and we release an existing shed. Failing toward
   comfort is deliberate: the cost of not shedding is money, the cost of wrongly shedding is a humid
   house, or an unfiltered one, that nobody is home to notice.

   The guardrail is **each device's own control metric**, not one global number: RH for the
   dehumidifier, PM2.5 for the air purifier. That is not tidiness — a single RH ceiling applied to the
   purifier is permanently unverifiable (it reports no humidity), so the purifier would silently never
   shed while the lane looked healthy. And the guardrail that actually matters for a purifier during a
   Pacific Northwest heat wave is smoke, not damp.

3. **The override outlives us, but not the window.** Every shed is written as a TTL sized to end exactly
   at the window end, and re-asserted each run. So if this lane dies mid-event — box down, network gone,
   crash loop — the shed still expires on time by itself. Nothing here can strand the house.
"""
from __future__ import annotations

from dataclasses import dataclass

# Actions the runner can be told to take for one device.
SHED = "shed"          # write an override off, expiring at the window end
RELEASE = "release"    # clear the override this lane set
HOLD = "hold"          # do nothing


@dataclass(frozen=True)
class ShedWindow:
    """A curtailment window from a signal source. `start`/`end` are epoch seconds."""
    start: float
    end: float
    reason: str            # human-legible: "PGE peak event 17:00-21:00" / "Excessive Heat Warning"
    source: str            # which source produced it: "schedule" | "nws" | "imap" | "manual"

    def active(self, now: float) -> bool:
        return self.start <= now < self.end


@dataclass(frozen=True)
class Decision:
    action: str            # SHED | RELEASE | HOLD
    reason: str
    duration_min: float | None = None   # SHED only: minutes until the window end


def _ours(live_expiry: float | None, our_expiry: float | None, tol: float = 90.0) -> bool:
    """Is the override currently on the device the one this lane wrote?

    Matched on expiry rather than a marker because the override row has nowhere to carry a marker — the
    control API stores (action, expiry) and nothing else. `tol` absorbs the drift between the expiry we
    asked for and the expiry the server computed from its own clock. A human override set to land within
    ~90s of ours would be misread as ours; that is a narrow enough window to accept, and the failure is
    benign (we release a pause that was about to expire anyway)."""
    if live_expiry is None or our_expiry is None:
        return False
    return abs(live_expiry - our_expiry) <= tol


def decide(now: float, window: ShedWindow | None, value: float | None, ceiling: float | None,
           live_expiry: float | None, our_expiry: float | None) -> Decision:
    """Resolve one device.

    `live_expiry` is the expiry of the override currently on the device (None = no active override).
    `our_expiry` is the expiry this lane recorded for the shed it believes it set (None = we set none).
    `value` is the guardrail reading in the DEVICE'S OWN control metric (RH for the dehumidifier, PM2.5
    for the purifier) — None means missing or stale, and the caller applies its own staleness rule before
    passing it in. `ceiling` None means this device was explicitly configured with no guardrail, in which
    case it sheds on the window alone."""
    mine = _ours(live_expiry, our_expiry)

    # 1. a human owns this device — hands off entirely
    if live_expiry is not None and not mine:
        return Decision(HOLD, "a manual override is active — leaving it alone")

    shedding = mine and live_expiry is not None

    # 2. no window, or the window has passed
    if window is None or not window.active(now):
        if shedding:
            return Decision(RELEASE, "shed window ended")
        # our_expiry recorded but nothing live: it already self-expired, just forget it
        if our_expiry is not None:
            return Decision(RELEASE, "shed window ended (override already expired on its own)")
        return Decision(HOLD, "no active shed window")

    # 3. guardrail unverifiable -> never shed, and undo one in progress
    if ceiling is not None and value is None:
        if shedding:
            return Decision(RELEASE, "guardrail reading unavailable — releasing (cannot verify)")
        return Decision(HOLD, "guardrail reading unavailable — not shedding")

    # 4. guardrail tripped -> veto, or abort a shed already running
    if ceiling is not None and value is not None and value >= ceiling:
        if shedding:
            return Decision(RELEASE, f"{value:.0f} >= ceiling {ceiling:.0f} — aborting shed")
        return Decision(HOLD, f"{value:.0f} >= ceiling {ceiling:.0f} — shed vetoed")

    # 5. shed, sized to end exactly at the window end
    mins = (window.end - now) / 60.0
    if mins <= 0:
        return Decision(HOLD, "window ends now")
    verb = "holding" if shedding else "shedding"
    guard = "no guardrail" if ceiling is None else f"{value:.0f} < {ceiling:.0f}"
    return Decision(SHED, f"{verb} until window end ({window.reason}); {guard}", duration_min=mins)
