# esp32s3-shades — Gaposa shade-panel node (ADR-0041)

Drives a **Gaposa QCTZ36SDU** dry-contact panel: 6 shade channels × Up/St/Dw = 18 contacts, closed by
three ULN2803A arrays from an ESP32-S3. The panel owns the 434.15 MHz radio and is already paired to the
motors — this node only closes contacts.

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

S3 GPIO → ULN2803A input → output sinks the QCT terminal to `com` (16.55 V rail, ~15 mA per opto LED).
Sinking **inverting** driver, so GPIO HIGH = contact asserted → `ha_dout` runs `active_high = true`.

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

## Build

```sh
cd edge/esp32s3-shades
cp main/secrets.example.h main/secrets.h     # then edit, or provision NVS
idf.py set-target esp32s3
idf.py build
```

## Not yet verified on hardware

- **`pulse_ms` = 500 is a guess.** The panel's minimum recognised width is undocumented — sweep from
  ~100 ms once a motor is paired and set the real value.
- **`interim_hold_ms` = 3000** is inferred from the handheld remote, not documented.
- **How the panel's error state clears** (release vs power cycle) is unresolved, and decides whether a
  firmware bug is self-healing or a trip to the box.
