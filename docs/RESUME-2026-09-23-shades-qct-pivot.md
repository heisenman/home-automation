# RESUME 2026-09-23 — shades: QCT drive topology pivoted to Atmel-direct

Session end. Tree clean, `origin/main` current. **The headline is a corrected sign and the design change
that followed from it.**

## The one thing to know

**`com` on the QCT is the rail POSITIVE, not the negative.** The 2026-09-21 entry for open question #15
recorded the sign backwards, and *everything* built on it followed — "low-side switching is correct", the
NPN-per-channel decision, the entire 3 × ULN2803A build spec, the parts bought, the boards soldered.

It surfaced physically: bonding `com` to the ULN emitters forward-biased every channel's
collector-substrate diode, lit all eighteen optos at once, and put the panel into its error state.

> **Why it survived review:** the manual says *"make a momentary closure between UP and Com."* A
> mechanical dry contact is **polarity-agnostic** — the source document could not have flagged a polarity,
> because mechanically there isn't one. The error entered when a grounded semiconductor was substituted
> for a floating contact. Worth remembering as a class of bug, not a one-off.

## Where the three nodes stand

| Node | State |
|---|---|
| `hvac_c6` | Live, LISTEN_ONLY ERV sniffer. Unchanged. Awaiting J9 measurements (§7.2). |
| `dehum_c6` | Live, inert. Unchanged. Awaiting §7.3 `DH`–`DH` measurement. |
| `shades_s3` | **Running the OLD pin map** (`4780430`). The harness-matched map + corrected pin-test labels are committed at `3a44e32` and **built but NOT flashed** — the board was off USB. |

⚠️ **First job next session: plug the UART cable in and flash `shades_s3`.** The map on the board does
not match the installed harness.

## The decision — Atmel-direct

Full rationale in the design doc, §3 *"DECIDED 2026-09-23 — drive the QCT's Atmel pins directly"*.
Short version: **the 16.55 V input section is abandoned, not fixed.** Measured inside: Atmel QFP44 on
5 V, each PC817A collector on an Atmel pin pulled up to 5 V, emitter at Atmel ground, **every input
active-low**.

Per channel: remove the opto, bridge terminal-side LED-anode pad → transistor-side collector pad.

⛔ **And cut the rail feed to `Com`, repurposing it as ground.** Non-negotiable — otherwise following the
*documented* "short UP to Com" procedure puts 16.55 V on a 5 V pin and kills the MCU. Label the enclosure.

**Nothing in firmware changes.** `active_high = true`, `kPin[]`, the harness, `ha_gaposa`, `ha_dout` and
the pin test all survive. The ULN boards become correct for the first time — a low-side sink pulling a
pulled-up logic line to ground is exactly what they are for.

Alternatives priced and rejected: photoMOS ~$60 (buys isolation we don't need), linkIT-US24 ~$270
(**materially the better product** per §5 — one UART, 24 channels, over-the-wire pairing, ACKs — but
writes off the QCT).

## Next session, in order

1. **Flash `shades_s3`** — new map is built and waiting.
2. **Verify pads on channel 1 before soldering**, power off: collector = few kΩ to 5 V; emitter ≈ 0 Ω to
   ground.
3. Remove 18 optos (no hot air on site — cut packages off with flush cutters, clean leads individually),
   bridge each, cut the `Com` rail feed, tie `Com` to Atmel GND, label the enclosure.
4. **Update `test_describe()`** — it still says *"terminal should DROP ~16.2V → ~1V"*. Becomes
   **"Atmel pin drops 4.8 V → ~0.7 V"**.
5. Re-run `tools/shade_cmd.py pintest` against the real hardware.

## Still open

- **#15** reopened and corrected; the ULN build spec is marked SUPERSEDED but retained for the harness.
- **#2** partially answered: **a power cycle clears the panel error** — it is recoverable, not brickable.
  Whether it *also* clears on release is undistinguished (AC was cut). Answer deliberately once a working
  drive exists: hold one contact >30 s, release without cutting power.
- **#3** minimum pulse width — still an unmeasured guess at 500 ms.
- **#4** interim recall via ~3 s hold — gesture and threshold each documented, but that a maintained
  closure reproduces a handheld's held STOP is still inferred.
- **Motors unpaired.** §3.6 — QCT `SEL` → hold `SYNC` → matching direction within 5 s. No handheld
  required. ⚠️ **One motor powered at a time** — pairing is broadcast RF.

## Lessons worth keeping

- **A polarity recorded once propagates into everything.** One flipped sign survived three days, a build
  spec, a parts order and a soldering session.
- **Verification tools must not lie.** The pin test derived its chip/pad labels from the step index,
  which assumed an ordering the harness didn't use. It would have announced the wrong pad *while being
  trusted to verify wiring*. Now a table beside `kPin[]`, row for row.
- **Don't pipe `mosquitto_sub` through `grep | head` under `timeout`** — grep block-buffers and the
  SIGTERM kills it before the flush. Produced two false "the node is dead" readings.
- **Ask what the vendor intended.** The QCT's dry contacts are meant for a Lutron/Control4 *relay card*.
  Recognising that is what reframed the whole problem away from "better transistor".
