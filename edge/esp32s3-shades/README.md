# esp32s3-shades — Gaposa shade-panel node (ADR-0041)

Drives a **Gaposa QCTZ36SDU** dry-contact panel: 6 shade channels × Up/St/Dw = 18 contacts, closed by
three ULN2803A arrays from an ESP32-S3. The panel owns the 434.15 MHz radio; motors are paired to it from
its own buttons (design §3.6). This node only pulls lines.

## Status — firmware COMPLETE (2026-09-27)

| | |
|---|---|
| Running | **`v4-shades`**, OTA'd from ha-2, self-test PASS. Parked **offline** (QCT powered down) until install. |
| Pin map | **Measured end to end.** GPIO→ULN pad by meter; pad→QCT channel/function by the panel's LEDs (repeated); final channel-order pintest matched all 18 as predicted. Open question #19 closed. |
| Motor path | **Proven on CH1.** A signed `shade 1 up` moved a panel-paired XS40 exactly like the panel's own UP button. 500 ms pulse is recognized. |
| Remaining | Install-side only (mount → direction → limits → pair CH2–6 → calibrate); see *Not yet verified* below. |

Re-check any of this with [runbook Entry 2](../../docs/runbook-device-verification.md#entry-2--shade-node-shades_s3-runs-the-verified-map-and-drives-the-qct).

**Design:** [hvac-shade-device-integration §3](../../docs/design/hvac-shade-device-integration.md) ·
**ADR:** [ADR-0041](../../docs/adr/ADR-0041-hvac-shade-actuator-integration.md)

## What lives where

`app_main.c` is a thin platform shim by design (ADR-0020) — it makes no protocol or policy decisions:

| Concern | Owner |
|---|---|
| Command planning, interlocks, position model | [`ha_gaposa`](../../firmware/components/ha_gaposa/) |
| Per-line fail-safe + hard max-on cap | [`ha_dout`](../../firmware/components/ha_dout/) |
| Signed commands, OTA, publish topics | [`ha_mqtt`](../../firmware/components/ha_mqtt/) |
| GPIO, NVS calibration, task wiring | this build |

If the control loop grows an `if` about shades, it belongs in `ha_gaposa` instead.

## ⛔ The hazard this node is built around

The QCT panel **latches an error — all LEDs on, transmission stops entirely** — if a contact is held past
30 s, or if two channels are driven with *different* commands at the same instant. `ha_gaposa` serialises
mixed commands and bounds every pulse; `ha_dout` enforces a **second, independent** cap per line
(`LINE_MAX_ON_MS`, 6 s) so a wedged planner still cannot hold a contact. Both layers are deliberate — do
not remove one because the other exists.

## Hardware

**Atmel-direct (decided 2026-09-23, built 2026-09-24).** The QCT's 16.55 V opto input section is bypassed:
all 18 PC817 optos were removed and bridged, so each ULN2803A output sinks an **Atmel input pin that the
QCT pulls up to 5 V** (active-low; 4.8 V → ~0.6 V when asserted). The `Com` rail feed is **cut** and `Com`
is tied to QCT PCB GND. Ground is one node: S3 GND = ULN pin 9 = QCT GND = `Com`. The ULN is a
**sinking, inverting** driver, so GPIO HIGH = line asserted, and `ha_dout` runs `active_high = true`.

⛔ **ULN `COM` (pin 10) stays FLOATING.** Its clamp diodes run output→`COM`. Tied to ground, they would clamp
every Atmel pin low, so every channel would be permanently asserted. If it is ever connected, it goes to +5 V.
⛔ **The panel's documented "short UP to Com" procedure is now UNSAFE on this unit.** `Com` is ground now.
The enclosure must say so. Rationale is in design §3, *"DECIDED 2026-09-23"*.

**The pin map is irregular on purpose.** The harness is straight (U2, U1, U3, each pad 6C→1C). The QCT's
*own* Atmel pad layout is not per channel: CH4–CH6 interleave across U1/U3. `kPin[]`/`kHarness[]` in
`app_main.c` carry that. Do not "tidy" them. They are measured, and the pintest is how to re-prove them.

⚠️ **Pin choice is not arbitrary.** GPIO 26–32 are the SPI flash and **33–37 are consumed by the N16R8's
octal PSRAM** — wiring there crashes the chip on boot. Strapping pins (0/3/45/46) must never drive a shade
contact: a reset glitch would fire a command on every boot and every OTA. Full pin table and the
two-channels-per-chip allocation are in the design doc.

## Commands

Signed (ADR-0010) on `home/edge/<node>/cmd`, dispatched via `ha_mqtt`'s `on_cmd` hook:

```json
{"op":"shade","ch":1,"cmd":"up"}          // ch 1..6, or 0 for ALL. cmd: up|down|stop|interim
{"op":"shade_cal","ch":1,"up_ms":25000,"down_ms":22000}
```

`ch:0` fans one command across every channel, which `ha_gaposa` batches into a single transmission — the
efficient path the panel explicitly allows for same-command groups.

**Travel times are NVS, not compile-time.** They are a property of the installation and differ per
direction (gravity assists DOWN), so calibrating a shade never requires a rebuild. Unset means position
reports `unknown` rather than a fabricated zero.

## Telemetry

`home/edge/<node>/shade<N>/adv`, device_type `shade`:

```json
{"position_pct":42.0,"position_confidence":"estimated","commanded":"down","moving":false}
```

**Position and commanded are separate fields with an explicit confidence**, per ADR-0041. The link is
one-way — the panel has no output contacts and the motor reports nothing — so anything between the limits
is dead reckoning. Never persist the estimate as though it were a measurement.

## ⚠️ Why this build enables Bluetooth it never uses

`CONFIG_HA_MQTT_NO_BLE=y` compiles every BLE use out of `ha_mqtt`, including the OTA radio-pause seam —
which matters, because `ha_ble_scan_resume()` calls `start_scan()` and this node never initialises NimBLE.

But `ha_gatt`/`ha_gatt_exec`/`ha_ble_scan` still appear in `EXTRA_COMPONENT_DIRS` and `CONFIG_BT_ENABLED`
is on, because **IDF expands component `REQUIRES` (build.cmake:686) before it generates sdkconfig
(build.cmake:705)**, in a child `cmake -P` that sees neither `CONFIG_` symbols nor project-scope
variables. So `ha_mqtt`'s dependency list cannot be made conditional, and those components must exist and
compile. They are linked and never called.

The clean fix is to move the BLE ops out of `ha_mqtt` and into the BLE nodes' own `on_cmd` hooks — the
mechanism now exists — which would leave `ha_mqtt` genuinely transport-only. Deferred: it changes three
live builds and wants hardware to verify.

## Build + deploy (on `.210`: edge secrets live there)

**OTA (normal path).** Bump `HA_FW_VERSION` in `main/app_main.c`, then:

```sh
bash tools/ota_edge_node.sh shades_s3 esp32s3-shades v<N>-shades
```

It re-emits `secrets.h` from the manifest, brands `version.txt`, builds, scp's to ha-2 and pushes a signed
OTA from there. The node pins its OTA host to ha-2 (`192.168.1.210`), so serving from `.210` is refused.
Success looks like `status: online ota_X v<N>-shades` + `OTA self-test PASS`, ~20 s after the write.

⚠️ **WiFi is COMPILED IN on this node, not in NVS.** The script now takes WiFi from this board's own
`main/secrets.h` and refuses an empty-WiFi build. Before that guard existed, the first v2 OTA shipped an
empty-WiFi image. It could not join the network, restarted while `PENDING_VERIFY`, and the bootloader
rolled it back. That looked exactly like a self-test failure. Keep WiFi in this board's `secrets.h`.

**Cable (first flash / recovery).** CH340 bridge on `/dev/ttyUSB0` (the port survives any firmware state):

```sh
cd edge/esp32s3-shades && idf.py -p /dev/ttyUSB0 flash monitor
```

## Verify — the pin test

`python3 tools/shade_cmd.py pintest` (interactive: Enter = next). It asserts **one line at a time** for
≤18 s and announces `step N/18: <ULN pad> | GPIO<n> ... [believes CH<c> <fn>]`. Since v4 the walk is in
channel order, so the panel's channel LEDs must read **`1U 1S 1D · 2U 2S 2D · … · 6U 6S 6D`**. Any break in
that sequence is a mapping or wiring fault. The pin test reads `kPin[]` directly, so it verifies whatever map is
flashed. Re-run it after any harness or `kPin[]` change, and never trust the tables' comments instead.

## Not yet verified on hardware (install-side; design §8 ledger)

- **`pulse_ms` = 500 is RECOGNIZED (proven 2026-09-27), but it is not the minimum** (#3). A sweep down from ~100 ms
  needs a runtime pulse knob, since the value is compile-time today. 500 ms works, so this is optimisation, not a blocker.
- **`interim_hold_ms` = 3000** (#4): the gesture and threshold are documented. That a maintained `St`
  closure reproduces a handheld's held STOP is still inferred.
- **Panel error clear** (#2): a power cycle clears it (observed). Whether release alone does is untested.
- **`Prog/FC` = LIMIT** is LIKELY (French *fin de course*), not yet exercised. `Prog/TX` = SYNC is CONFIRMED.
- **Travel times** (#12) are per-shade and post-install: `shade_cmd.py cal <ch> <up_ms> <down_ms>` → NVS.
- **Telemetry** (`shade<N>/adv`) has not been read back live with a calibrated shade. Position stays `unknown` until then.
