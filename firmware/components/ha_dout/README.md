# ha_dout — dry-contact / relay output driver

**Contract:** [`include/ha_dout.h`](include/ha_dout.h) · **ADR:** [ADR-0041](../../../docs/adr/ADR-0041-hvac-shade-actuator-integration.md)
· **Design:** [hvac-shade-device-integration](../../../docs/design/hvac-shade-device-integration.md)

Decides what an on/off output should be doing. It never touches a GPIO — the caller reads
`ha_dout_level()` and applies it. That keeps the module pure (no ESP deps, host-tested) and lets the same
logic sit behind a GPIO, an I²C expander, or a PhotoMOS array without a fork.

> ⚠️ **Not `ha_relay`.** That name is taken by the BLE advert relay-coverage filter (ADR-0015), which is
> unrelated to physical relays. This is the one that closes contacts.

## Why it exists

Three appliances in ADR-0041 need a contact closed, and all three punish a stuck-ON differently:

| Consumer | Output | Stuck-ON consequence |
|---|---|---|
| `ha_aprilaire_dehum` | call-for-dehumidification on `DH`/`DH` | compressor runs indefinitely |
| `ha_gaposa` | shade UP / STOP / DOWN | **the panel latches an error past 30 s and stops transmitting entirely** |

So the module is built around the failure, not the happy path.

## What it guarantees

- **Fail-safe is always logical OFF.** Uninitialised, faulted, watchdog-tripped — every giving-up path
  lands on OFF. Wire the load so OFF is harmless (a normally open contact), and an unpowered or crashed
  node leaves the appliance not-calling.
- **Short-cycle dwell.** `min_on_ms` / `min_off_ms`. A command blocked by dwell is **deferred, not
  dropped** — it lands on the tick after the interval expires, so a call for dehumidification is never
  silently lost.
- **Hard max-on cap.** `max_on_ms` forces OFF and **latches a fault**. Clearing is deliberate
  (`ha_dout_clear_fault`) because an overrun means something upstream wedged, and silently resuming would
  hide it.
- **Momentary pulses.** `ha_dout_pulse()` for the shade primitive. Out-of-envelope widths are **rejected,
  not clamped** — silently shortening a shade command, or stretching one past the panel's 30 s lockout,
  turns a config error into a field mystery.
- **Wrap-safe timing.** Every comparison goes through an unsigned difference. `esp_timer` milliseconds
  wrap every ~49.7 days and these nodes run far longer between reboots; naive `>=` would fire every
  deadline at once, or stall one forever, exactly once per wrap. Covered by tests.

## The one thing it cannot do alone

`ha_dout_tick()` is a **software** watchdog. If the loop calling it stops, nothing forces the contact off.

Where a stuck-ON is genuinely harmful, the platform adapter must use `ha_dout_next_deadline_ms()` to arm
an `esp_timer` one-shot that drives the pin to the inactive level **directly**. That is the only form of
the cap that survives a wedged main loop, and the header says so rather than implying the tick is enough.

## Platform support

Pure C99, no `REQUIRES`. Identical on c3/c6/s3/p4 — there is no board seam, because there is no hardware
in it.

## Test

```sh
firmware/components/ha_dout/test/run.sh    # plain cc, no IDF
```

Covers polarity, fail-safe reads on an uninitialised struct, deferred commands in both directions,
pending-command cancellation, pulse envelope rejection, level-supersedes-pulse, watchdog trip + latch +
clear, deadline reporting (including the cap outranking a longer pulse), and millisecond wraparound for
pulses, the watchdog, and dwell.
