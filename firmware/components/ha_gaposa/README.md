# ha_gaposa — Gaposa shade-panel command planner

**Contract:** [`include/ha_gaposa.h`](include/ha_gaposa.h) · **ADR:** [ADR-0041](../../../docs/adr/ADR-0041-hvac-shade-actuator-integration.md)
· **Design:** [hvac-shade-device-integration §3](../../../docs/design/hvac-shade-device-integration.md)

Drives a Gaposa **QCTZ3SDU / QCTZ36SDU** dry-contact shade panel. The panel owns the 434.15 MHz radio and
is already paired to the motors — we only close contacts. Pure planner: no GPIO, no deps, host-tested.
Pair the outputs with [`ha_dout`](../ha_dout/README.md).

## Why a planner and not just three GPIOs

The panel is a 6-channel box with `Up` / `St` / `Dw` against each channel's `com`, and a **momentary**
closure issues a command — the motor then runs to its own stored limit. That part is simple. Two things
are not:

**⛔ The box-wide interlock.** The panel latches an error — all LEDs on, **transmission stops entirely** —
if two channels are driven with *different* commands at the same instant. The manual's own example is CH3
DOWN together with CH4 UP. Same command across many channels is fine, and is the efficient path for
"all up".

This is easy to trip by accident: a scene like *"close everything except the office"* is DOWN on five
channels and UP on one, which is exactly the forbidden shape. So this module **serialises mixed commands
into separate time slots** with a guard gap, rather than trusting the caller to remember. Commands of the
same type still go out together in one batch.

**The 30 s lockout.** A closure held past 30 s trips the same error state, so a stuck output does not
merely run a shade — it disables the whole panel. Every pulse here is hard-bounded at
`HA_GAPOSA_MAX_PULSE_MS` (5 s), and out-of-envelope config is **rejected at init rather than clamped**.
Feed the outputs through `ha_dout` so the cap also survives a wedged main loop.

## ⚠️ There is no feedback. At all.

The panel has no output contacts, the RF link is one-way, and the motor reports nothing — Gaposa's own
cloud API does not expose position either. Position here is **dead reckoning from travel time** and it
drifts with every command.

The API makes you confront that. `ha_gaposa_position()` returns a confidence alongside the value:

| Confidence | Meaning |
|---|---|
| `UNKNOWN` | never driven to a limit since boot, or no travel time configured, or the motor was sent to its stored intermediate position (which we cannot express in per-mille) |
| `ESTIMATED` | dead reckoning — moving, or stopped mid-travel |
| `EXACT` | parked against a motor-side limit after a completed run |

Only three states are genuinely absolute: fully open, fully closed, and the motor's stored intermediate
position. The first two re-datum the estimate on every full run, which is why driving to a limit is worth
doing periodically.

`ha_gaposa_commanded()` is kept deliberately separate from `ha_gaposa_position()`: what we *told* the
shade to do and where we *think* it is are different facts, and ADR-0041 requires storing them as such.
Never persist the estimate as though it were a measurement.

## Not yet verified on hardware

- **Minimum recognised pulse width is UNKNOWN** — undocumented. The 500 ms default is a starting guess;
  sweep it on the bench.
- **`CMD_INTERIM`** (hold `St` ~3 s to recall the stored intermediate position) is inferred from the
  handheld remote's behaviour, not documented. It is batched separately from `CMD_STOP` so a short stop
  and a long hold can never overlap on the same wire.
- **How the panel's error state clears** — release or power cycle? — is unresolved and needs a bench test.

## Test

```sh
firmware/components/ha_gaposa/test/run.sh    # plain cc, no IDF
```

The suite steps the planner at 10 ms granularity through every scenario and asserts the box-wide
invariant **at every tick**, not just at the end: at most one command type asserted across the box, and
each channel's assertion matching the batch. Covers config rejection, single commands, the mixed-direction
serialisation with batch order and membership, same-command batching, all three types queued at once, the
guard gap boundaries, newest-intent-wins, the full position/confidence lifecycle (unknown → estimated →
exact → demoted by motion → estimated after a mid-travel stop → unknown after interim), the unconfigured
travel-time case, and millisecond wraparound.
