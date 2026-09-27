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

---

## UPDATE 2026-09-24 — modification BUILT, map flashed, node live

Hugh completed the Atmel-direct modification: **18 optos removed and bridged, `Com` rail feed cut and
`Com` jumped to PCB GND.** Lines and continuity checked before power-up. The screw terminal block came
off too — wires land directly on the board pads.

**`shades_s3` now runs the harness-matched map.** That closes the "first job next session" item above.

```
home/edge/shades_s3/status  online ota_0 v1-shades
pintest: ASSERTED step 1/18: U2 4C | CH1 Up | GPIO15  (Atmel pin should DROP 4.8V -> ~0.7V)
pintest: ASSERTED step 2/18: U2 5C | CH1 St | GPIO7   ...
```

Grounds now one node: **S3 GND = ULN pin 9 = QCT PCB GND = QCT `Com` pads.**

⛔ **ULN `COM` (pin 10) stays floating — asked and answered for the THIRD time.** The diodes are
anode-at-output, cathode-at-`COM`, and the outputs now sit at 4.8 V pulled up to the Atmel rail. Tying
`COM` to the ground node forward-biases all eighteen and clamps every Atmel pin to ~0.7 V — every channel
permanently asserted, transistors idle. Same mechanism as the 16.55 V flashing-lights event. `COM` is not
a ground and does not become one no matter what else is bonded. If ever connected, it goes to **+5 V**.

### Next

1. **Run the pin test against real hardware** — `python3 tools/shade_cmd.py pintest`, watch each Atmel
   pin drop 4.8 V → ~0.7 V in the announced order. This is the first end-to-end confirmation of the
   modification.
2. **Label the enclosure** — nothing on the board matches its own silkscreen now.
3. Pair the motors (§3.6, one motor powered at a time), then §3.3 pulse-width sweep.

### Unchanged and still open

`hvac_c6` (LISTEN_ONLY, awaiting J9) and `dehum_c6` (inert, awaiting §7.3) both healthy and untouched.
Open #2 / #3 / #4 as recorded above.

---

## UPDATE 2026-09-27 — the modification WORKS. Firmware v2 written but NOT running (OTA rolled back)

QCT and S3 reassembled, back on AC, smoke test passed. **Hugh ran the full 18-step pin test with a meter.**

### ✅ The Atmel-direct modification is electrically PROVEN

Every one of the 18 lines pulled its Atmel pad from 4.8 V down to **~0.6 V**, in the announced order. That
is the end-to-end confirmation the last three sessions were building toward: 18 optos removed, 18 bridges,
the `Com` rail cut and repurposed as ground — all of it good. **This question is closed.**

### ⚠️ But two datasets disagree, and only one of them is about pads

| What was measured | Result |
|---|---|
| **ULN output pads** (meter) | `U2 4,5,6,1,2,3` · `U1 4,5,6,1,2,3` · `U3 4,5,6,1,2,3` — in walk order |
| **QCT channel LEDs** (eye) | `1U 1S 1D · 2S 2D 2U · 3D 3S 3U · 4S 4U 4D · 5D 6S 5U · 6U 6S 6D` |

**The pad data corrected a mis-record and made the harness uniform.** U3 had been documented `1->6` since
2026-09-23; it is actually `6->1` like the other two. Same provenance as the `com` polarity error — a
by-hand report, replaced by a measurement. Every `kPin[]` row now reads `4C/5C/6C` then `1C/2C/3C`, which
is the tell that the table is self-consistent.

**The LED data says the pad→function mapping is wrong on four channels.** Right channel, wrong function
inside it, per-channel permutations. And step 14 lit `6S` where `5S` was expected — `5S` never lit, `6S`
lit twice, so that pair is either a misread or two wires on one pad. Leading candidate: per-wire
transposition, since the 18 ULN→Atmel wires were landed individually by hand. **Logged as open question
#19.** Hugh's call was to fix the pad sweep first and chase the QCT afterwards, which is the right order —
one unknown at a time.

⛔ **Do NOT issue real `shade` commands yet.** `kPin[]`'s Up/St/Dw columns are a belief; a CH5 command may
actuate CH6. Motors are still unpaired so nothing can physically move, which is the only reason this is
merely wrong and not dangerous.

### Firmware v2 (committed, built, warning-free) — four changes

1. **`kHarness[]` U3 rows corrected** to `4C/5C/6C` + `1C/2C/3C`.
2. **New `kWalk[]` table — the pin test now sweeps PADS, not channels.** `U2 6C→1C, U1 6C→1C, U3 6C→1C`:
   one continuous left-to-right progression across the three chips, so the next expected pad is always the
   one physically beside the probe and a transposition shows up as a break in an obvious sequence. Walk
   order is now *separate* from `kPin[]` — `kPin` is indexed by what a line means, `kWalk` by the order a
   human wants to be shown it.
3. **`test_describe()` leads with the pad and labels the channel/function `[believes CH5 St]`.** The pad is
   measured, the function is not, and the operator must be able to tell them apart. Also `~0.7V` → `~0.6V`
   to match the meter.
4. **The control loop compares against `kWalk[]`, not `ch * 3 + fn`** — a formula there would assert a
   different line than `test_describe()` just announced. Same class of bug as the 2026-09-23 label formula.

### ⛔ HANDOFF STATE: the node is running v1-shades, NOT v2

**The OTA rolled back.** `shades_s3` is on `ota_0 v1-shades` — the OLD pin map and the OLD U3 labels. If you
run `pintest` right now you get the old walk order and the wrong U3 pad names.

Yes, this node is OTA-capable and the recipe is the right one:

```sh
bash tools/ota_edge_node.sh shades_s3 esp32s3-shades v2-shades
```

What happened — it got **all the way through** and then reverted:

```
identity OK: image 'shades_s3@v2-shades' is built for 'shades_s3'
OTA image hash verified
OTA write OK — rebooting into ota_1 (pending verify)
status: offline
status: online ota_0 v1-shades
✗ OTA ROLLED BACK — bad image failed self-test; node reverted (safe)
```

**Diagnosis is UNFINISHED.** What is established:

- **Not a rejection.** Host pin, identity gate and signed hash all passed; the image was written.
- **The self-test is `ha_mqtt_is_connected()` within ~15 s** (`ha_ota_confirm_if_pending`, 30 × 500 ms).
  In `app_main` it is called *immediately* after `ha_mqtt_start()`, so the trial image gets ~15 s to join
  WiFi's already-up radio and reach the broker.
- **The trial boot cannot report why it failed.** `ota_log()` publishes over MQTT — the exact thing that
  wasn't up. Absence of `ota_1` log lines is expected and is NOT evidence of a crash.
- **Serial is no longer available** — the UART is unplugged now that the node is in the enclosure.
- The v2 build is warning-free and the changes are static tables plus indexing, so a boot crash is
  possible but not the obvious suspect. Untested either way.

Candidates, in rough order: (a) 15 s is simply too tight on this net; (b) something before
`ha_mqtt_start()` fails on a trial boot and restarts — `ha_wifi_connect` failure sleeps 10 s then
`esp_restart()`s, and a reboot while `PENDING_VERIFY` makes the bootloader revert on the next boot, which
would look exactly like this; (c) a real fault in v2.

Cheapest way to separate them: **time the `offline`→`online` gap on a re-attempt.** ~20 s means the
self-test ran its full wait and timed out; ~5–8 s means it restarted early, pointing at (b).

### OTA facts worth not rediscovering

- **The OTA must be served from ha-2.** `ota_host_pinned_ok()` enforces URL host == `192.168.1.210`; this
  box is `192.168.1.245` on the air-gap leg, so serving locally is rejected node-side. `ota_edge_node.sh`
  already does the scp-then-run-there dance.
- ha-2 prerequisites all verified present: `~/ota-venv/bin/python3` (has paho), `~/ota-canary/`,
  `~/home_automation/tools/edge_ota.py`.
- `version.txt` is now `shades_s3@v2-shades` (gitignored). The identity gate wants `<node_id>@`, and
  re-OTA of the same tag is fine — the gate is identity-only, not version-monotonic.

### Next session, in order

1. **Re-attempt the OTA and time the `offline`→`online` gap.** Decides (a)/(b) vs (c) without a cable.
2. If it needs a cable: the UART goes back on, `idf.py -p /dev/ttyUSB0 flash monitor` shows the trial boot
   directly — and a cable-flashed image is never `PENDING_VERIFY`, so it sidesteps the self-test entirely.
3. **Re-run the pin test on v2** and check the LED order against the pad sweep. Expected now: one clean
   progression. That is the measurement that resolves #19.
4. Then pair the motors (§3.6, one at a time — broadcast RF) and only then issue real `shade` commands.
5. Still waiting: label the enclosure; §3.3 pulse-width sweep; `hvac_c6` §7.2 J9; `dehum_c6` §7.3.

---

## UPDATE 2026-09-27 (later) — OTA root-caused and FIXED; `shades_s3` now runs v2

```
18:45:49Z  log     OTA write OK — rebooting into ota_1 (pending verify)
18:46:08Z  status  online ota_1 v2-shades
18:46:08Z  log     OTA self-test PASS — image confirmed valid on ota_1
```

**Root cause: the v2 image had EMPTY WiFi.** `tools/ota_edge_node.sh` regenerates `secrets.h` with
`enroll_node.py --reuse`, whose default `--base-secrets` is `edge/esp32c6/main/secrets.h`. That base
carries empty WiFi on purpose, because the gas fleet keeps WiFi in NVS. `shades_s3` (like `hvac_c6` and `dehum_c6`) was
cable-flashed with WiFi **compiled in** and has none in NVS. So the trial image failed `ha_wifi_connect`
(30 s), `esp_restart()`ed while `PENDING_VERIFY`, and the bootloader reverted. That is candidate (b) above. It was
never the 15 s self-test and never v2's code.

**Fixes:**
- `ota_edge_node.sh` now uses the board's OWN `secrets.h` as the WiFi base when it has one, and
  **refuses** to build an empty-WiFi image unless `WIFI_IN_NVS=1`. `hvac_c6`/`dehum_c6` would have hit
  the same trap on their first OTA.
- `HA_FW_VERSION` bumped to `v2-shades`. It still said `v1-shades`, so a good OTA would have misreported
  itself. Only the gitignored `version.txt` had been bumped.

**Next:** re-run `python3 tools/shade_cmd.py pintest` on v2 and compare the LED order to the pad sweep
(open question #19). Then pair the motors. Still no real `shade` commands until #19 is resolved.

## UPDATE 2026-09-27 (v3/v4) — QCT function map corrected from the LED run; pintest now walks CHANNELS

Pad-order pintest on v2, run TWICE by Hugh with the same result, LED sequence:
`1D 1S 1U · 2U 2D 2S · 3U 3S 3D · 4D 6U 4S · 4U 6S 5D · 6D 5S 5U` (pads U2 6C→1C, U1 6C→1C, U3 6C→1C).
That's a clean bijection. The v1 run's duplicate `6S` was a misread. Open question #19 is answered: the ULN→Atmel
harness is straight; the **QCT's own pad layout** is not per-channel. Fixed in `kPin[]`/`kHarness[]` (no
rewire). v3 shipped that map; v4 makes the pintest walk CH1..CH6 Up/St/Dw, so the predicted LED sequence
is `1U 1S 1D … 6U 6S 6D`. Both OTAs passed the self-test first time. **Awaiting Hugh's v4 channel-order run**
before any real `shade` command, then motor pairing (§3.6, one at a time).
