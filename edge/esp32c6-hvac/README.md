# esp32c6-hvac — Broan AI Series ERV node (ADR-0041)

Listens to a **Broan / NuTone / Venmar / vanEE / Best AI Series (AM1)** ERV on its `D+`/`D-` wall-control
bus from a **Seeed XIAO ESP32-C6**.

**Design:** [hvac-shade-device-integration §1](../../docs/design/hvac-shade-device-integration.md) ·
**ADR:** [ADR-0041](../../docs/adr/ADR-0041-hvac-shade-actuator-integration.md)

## ⛔ This build is listen-only

It parks on the bus beside the working wall control and **says nothing**. That is what makes it safe to
attach to a live ERV, and it is bring-up step 1 from ADR-0041 §1.12 — it validates wiring, polarity,
baud, checksum and the register map at *zero* bus writes.

Three things enforce it, and they are independent on purpose:

| Gate | Where |
|---|---|
| The state machine never produces a frame | `ha_broan` `listen_only` |
| The transport refuses every write | `ha_rs485` `listen_only` |
| The node drops anything that reaches it anyway, loudly | `bus_task()` gate-violation check |

All three come off one compile-time knob, `HA_HVAC_LISTEN_ONLY` (default `1`). **There is deliberately no
runtime override.** Taking this bus means taking the wall control's slot: from then on the ERV is owed a
heartbeat every 10 s, and going quiet past 5 s raises fault **E50 and the unit shuts down** — including
across every OTA. Whether E50 self-clears or needs a physical power cycle is **unresolved and blocking**
(ADR-0041 §1.8). Earning that should require a rebuild, not an MQTT message.

**Listen-only is not a degraded mode.** `ha_broan` caches register responses regardless of which
controller they were addressed to, so a node parked beside the wall control harvests genuine telemetry —
power, temps, CFM, RPM, fault codes — and publishes it normally.

## Wiring

Verified against the board silk on the bench, 2026-09-22.

```
XIAO D10 (GPIO18) ──TX──> Waveshare TTL TO RS485 (C)  RXD
XIAO D9  (GPIO20) <─RX─── Waveshare TTL TO RS485 (C)  TXD
XIAO 3V3          ───────>                            VCC     ⚠️ 3V3 only — C6 is not 5V tolerant
XIAO GND          ───────>                            GND
XIAO D8  (GPIO19) ───────> OVR relay IN                       active-high, 10k pull-down, NO contact

Waveshare A+ ──> Broan J9 `D-`      ⚠️ inverted, and correct
Waveshare B- ──> Broan J9 `D+`
Waveshare PE ──> Broan J9 `GND`     Waveshare calls PE "RS485 Signal Ground"
```

- ⛔ **Do not bond XIAO GND to Broan GND.** The converter is galvanically isolated; TTL-side GND serves
  the node, `PE` serves the ERV, and they must never meet.
- ⛔ **Do not go near J13** — that block carries 24 VAC for the furnace interlock and crossing it into J9
  permanently damages the control board. Meter J9 `12V`→`GND` first.
- **No termination resistor.** The bus already has whatever it has; the Waveshare's onboard 120 Ω is
  solder-enabled and verified open on our board (`A+`↔`B-` reads 10 kΩ, which is bias + receiver load).
- The transceiver is **auto-direction** — no DE/RE pin, so `de_gpio = -1`.

### Why RS-485 is on D10/D9 and `UART_NUM_1`

Not ergonomics. UART0 is GPIO16/17 = XIAO **D6/D7**, and `CONFIG_ESP_CONSOLE_UART_DEFAULT` makes it this
tree's **primary** console (USB-Serial-JTAG is only secondary). A transceiver there would push the ROM
bootloader banner and the full boot log onto a live ERV bus at every reset — and the listen-only gates are
application-layer, so none of them exists yet when the ROM bootloader is talking.

`uart_set_pin()` relocates the console *along with* UART0, so choosing `UART_NUM_0` re-creates the same
hazard on any pins. Use `UART_NUM_1`.

## Reading the sniff report

The two ways this wiring fails look identical to "the ERV is quiet" unless you watch the byte counters,
so the node interprets them itself and publishes the verdict to `home/edge/<node>/log` every 15 s:

| Symptom | Verdict |
|---|---|
| `rx=0 B` | Not hearing the bus at all. The Waveshare wiki's `RXD`/`TXD` labels are ambiguous — move the data-out wire to the other TTL pad. Then check A+/B-/PE are landed. |
| bytes, `frames=0` | **A/B swapped** (or wrong baud). Non-destructive; just swap them. |
| high `bad` rate | Marginal bus — a third terminator, or a noisy ground reference. |
| `frames>0`, low `bad` | Working. |

`writes_refused` should stay **0**: `ha_broan` never produces a frame under listen-only, so the transport
gate is never even reached. A non-zero count means the upper gate leaked.

Force a report without waiting for the tick with the signed command `{"op":"erv_stats"}`.

## Temperatures are published as `*_temp_raw` on purpose

Whether the ERV reports °C or °F is **unresolved** (ADR-0041 open question #6) and `ha_broan` does no
conversion. A field named `supply_temp` would be charted as °C by the first person to see it. Renaming
them once §7.2 settles the units is a deliberate one-time cost, taken while this node is still on a bench.

Fields are **omitted, never zero-filled**, when a register has not been seen — and `exhaust_temp_raw`
reads NaN on units without the second thermistor, which is not valid JSON at all.

## What lives where

`app_main.c` is a thin platform shim by design (ADR-0020) — it makes no protocol decision:

| Concern | Owner |
|---|---|
| Token handshake, heartbeat, polling, field cache | [`ha_broan`](../../firmware/components/ha_broan/) |
| Framing, checksum, register codec | [`ha_broan_frame`](../../firmware/components/ha_broan/) |
| Half-duplex transport, direction, write gate | [`ha_rs485`](../../firmware/components/ha_rs485/) |
| Signed commands, OTA, publish topics | [`ha_mqtt`](../../firmware/components/ha_mqtt/) |
| GPIO, task wiring, JSON assembly | this build |

**Broan only.** `ha_aprilaire_dehum` is not built and not linked — the component does not exist, and the
Aprilaire is moving to its own node (see "Open" below).

## Build and flash

```sh
cd edge/esp32c6-hvac
cp main/secrets.example.h main/secrets.h     # then edit
idf.py set-target esp32c6
idf.py build
idf.py -p /dev/ttyACM0 flash monitor
```

⚠️ **First flash must be by cable.** A fresh node can reject every OTA on a dangling `node_id`, and the
fix cannot ship over the air. Do a **real power cycle** afterwards — a DTR/RTS reset is not a power cycle.

⚠️ If you rebuild after copying files around, check the `.bin` really rebuilt — `cp -a` preserves mtime,
ninja skips the step, and the image silently keeps its previous identity.

### External antenna

The XIAO C6 antenna switch is software-controlled and defaults to the internal ceramic antenna. Build with
`-DHA_HVAC_EXT_ANTENNA` to drive GPIO3 low + GPIO14 high for the U.FL connector. ⚠️ Only with an antenna
actually attached — switching to an unconnected U.FL is worse than the ceramic one, and presents as an
inexplicable placement problem rather than a config error.

## Open

- **Server-side:** `abilities = "erv"` and the `erv` device_type are new. Intake may read the node as
  relay-only and the metrics will not chart until `METRIC_CATALOG` knows them — and a catalog edit needs
  **both** `ha-api` and `ha-api-tls` restarted.
- **Bench, before landing on J9:** design doc §7.2 — meter J9 `12V`→`GND` (confirm you are not on J13),
  and confirm XIAO and Broan grounds are not bonded.
- **Blocking for controller mode:** does E50 self-clear, or need a power cycle? (§7.2 step 6.)
- **Node split:** ADR-0041 currently has `ha-hvac` carrying both the ERV and the Aprilaire. That is under
  reconsideration — the two appliances are not necessarily near each other — and this build is already
  Broan-only, so a split costs nothing here.
