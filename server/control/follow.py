"""Follow coupling: a switch that turns on while another device runs (ADR-0043 §5 — Hugh, 2026-10-26: "whenever
the attic dehum turns on so does the blower"). Configured on the FOLLOWER's switchable trait:

    hvac_blower:
      traits:
        switchable: {label: Furnace blower, follow: {device: dehum_attic}}

EDGE-triggered, like LED night mode, so a person's choice always wins:
  - leader START: switch the follower ON — unless it is already on (a person's setting: left alone, and not
    ours to turn off later);
  - leader STOP: switch it back OFF — only if WE turned it on and it is still on.
A person who switches it off mid-run, or on before the run, is never overridden. State persists in control.db
(house_setting `follow:<follower>`) so a controller restart mid-run still hands the blower back.

Unknown leader or follower state (no fresh telemetry) -> do nothing this tick. A failed command keeps the old
state, so the edge is retried next tick.
"""
from __future__ import annotations

FRESH_S = 300.0           # telemetry older than this is "unknown"


def initial() -> dict:
    return {"leader_on": False, "engaged": False}


def decide(leader_on: bool | None, follower_on: bool | None, state: dict) -> tuple[bool | None, dict, str]:
    """-> (command: True on | False off | None nothing, state to save IF the command succeeds (or None), why).
    Pure."""
    st = {**initial(), **(state or {})}
    if leader_on is None or follower_on is None:
        return None, st, "leader or follower state unknown"
    if leader_on and not st["leader_on"]:
        if follower_on:
            return None, {"leader_on": True, "engaged": False}, "leader started; follower already on (left alone)"
        return True, {"leader_on": True, "engaged": True}, "leader started -> follower ON"
    if not leader_on and st["leader_on"]:
        if st["engaged"] and follower_on:
            return False, initial(), "leader stopped -> follower back OFF"
        return None, initial(), ("leader stopped; follower already off" if st["engaged"]
                                 else "leader stopped; follower was not ours (left alone)")
    return None, st, "no edge"
