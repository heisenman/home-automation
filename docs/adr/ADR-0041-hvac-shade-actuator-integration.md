# ADR-0041 — HVAC + shade actuators: capability seams over device seams

**Status:** Proposed — research complete, hardware arriving late Sept / early Oct 2026, nothing built yet
**Relates to:** ADR-0020 (shared firmware core / module-first), ADR-0014 (device-control conventions),
ADR-0034 (Node / Ability / Entity), ADR-0027 (actuator telemetry + area contract)
**Reference:** [docs/design/hvac-shade-device-integration.md](../design/hvac-shade-device-integration.md)
— protocol detail, register maps, terminal tables, bench checklists, sources

---

## Context — four appliances, four interfaces, one temptation

Four devices arrive in the next fortnight:

| Device | What it turned out to speak |
|---|---|
| Broan `IAQ-AM1-1-B160E75RS` ERV | RS-485, **proprietary Broan framing** — not Modbus, not BACnet |
| Aprilaire `E070` dehumidifier | **Dry contact**, `DH`/`DH`, electromechanical relay mandatory |
| Gaposa `QCTZ36SDU` shade controller | **18 dry contacts** (6 channels × Up/St/Dw) |
| Gaposa `XS40` tubular motor | 434.15 MHz, reachable only *through* the controller |

The temptation with a batch like this is to build a box per appliance: a "Broan driver", an "Aprilaire
driver", a "Gaposa driver", each owning its own UART or GPIO handling. That is the `cp -r` fork tax
ADR-0020 exists to prevent, one layer up — instead of forking a build, you fork the transport.

Four preliminary findings shaped the decision before any code was written, as the ADR-0020 mandate
requires.

### Finding 1 — the assumed protocol was wrong

The ERV was assumed to be Modbus RTU. It is not. Broan's 52-page installer manual contains **zero**
occurrences of "Modbus", "RS-485", "baud", or "0-10", and Broan declined to share specifications. The
Home Assistant community spent years cycling baud rates and parity against a CRC that does not exist —
the checksum is an 8-bit sum with deliberate overflow. The bus is **token-passed half-duplex**, not
master/slave polled.

Had we built `ha_modbus` first and hung a Broan profile off it, the abstraction would have been wrong at
the bottom.

### Finding 2 — the RF path was a trap we avoided by accident

The original plan for the shades was to emulate the Gaposa remote at 434.15 MHz. Research established
that (a) no `rtl_433` decoder exists — verified against a fresh clone, `grep -ri gaposa` over 331 device
decoders returns nothing; (b) the frame layout, sync word, bit rate and checksum are undocumented
everywhere; and (c) **whether Gaposa rolls its code is unknown**, which means a captured replay might
work exactly once. That is a project-killer discovered only after buying a transceiver and an SDR.

Hugh corrected the premise mid-research: the QCTZ36SDU is not a remote, it is a controller box that takes
dry-contact inputs and does the RF itself. The entire RF question dissolves.

### Finding 3 — taking the ERV's bus makes it *less* reliable

Only one controller may exist on the Broan bus (*"it will only ever respond to one device on the bus"*).
We do not coexist with the wall control; we **replace** it, and inherit its liveness obligation —
heartbeat every 10 s, 5 s token timeout. Drop off and the ERV raises **E50 "wall control communication
lost" and shuts down.**

Our node will drop off that bus on **every OTA**. Whether E50 self-clears or needs a manual power cycle
is undocumented and unresolved. A routine operation that might require a physical trip to the basement is
exactly the operator dead-end this project treats as a structural defect.

### Finding 4 — two of the three devices offer no feedback at all

The Gaposa link is one-way: no status contact, no position, and even Gaposa's cloud API does not expose
it. The Aprilaire has no status, fault, or alarm output — faults surface only on its LCD. Only the ERV
reports anything about itself.

For a project where stored sensor data is primary and device state is verified rather than trusted, a
write-only actuator is a hole, not a feature.

---

## Decision

### 1. Cut modules by capability, not by appliance

Three device profiles sit on **two** shared transports. Nothing device-specific lives in a transport;
no transport code lives in a device profile.

```
ha_dout ──┬── ha_aprilaire_dehum      ha_rs485 ──── ha_broan
          └── ha_gaposa
```

**Shared capability modules**

| Module | Contract |
|---|---|
| `ha_dout` | Dry-contact / relay output. Fail-safe state asserted **before** the pin becomes an output; non-strapping-GPIO requirement in the header; pulse primitive with a **guaranteed maximum width** enforced by a watchdog independent of the command path; minimum on/off dwell; commanded-vs-actual tracking. |
| `ha_rs485` | UART + half-duplex RS-485. **Optional** DE/RE GPIO in `ha_rs485_cfg_t` (`-1` = auto-direction), leaving ESP-IDF's native `UART_MODE_RS485_HALF_DUPLEX` available without forcing it. |

**Device-profile modules** — thin: register/command maps and semantics only.

| Module | Sits on |
|---|---|
| `ha_broan` | `ha_rs485` |
| `ha_aprilaire_dehum` | `ha_dout` |
| `ha_gaposa` | `ha_dout` |

`ha_dout` earns its place immediately: one tested module carries three appliances across two nodes. That
is the argument for capability seams stated as a fact rather than a preference.

⚠️ **Naming:** the existing `ha_relay` is the **BLE advert relay-coverage filter** (ADR-0015), entirely
unrelated. The new module is `ha_dout` specifically to avoid that collision.

### 2. The hosting decision was deferred by construction — and DECIDED 2026-09-20

`ha-hvac` = `ha_broan` + `ha_aprilaire_dehum`; `ha-shades` = `ha_gaposa`, mounted at the controller.

**Hugh's call, 2026-09-20 — two nodes, split by location rather than by appliance:**

| Node | Silicon | Carries | Sits |
|---|---|---|---|
| `ha-shades` | **ESP32-S3 N16R8** (dedicated) | `ha_gaposa` | inside the QCT enclosure |
| `ha-hvac` | **ESP32-C6** (shared) | `ha_broan` + `ha_aprilaire_dehum` | mechanical room |

The S3 is deliberate overkill on compute, chosen for **pin count**: 18 QCT channels wire directly with no
expander, so the hardware does not have to change later. ⚠️ N16R8 has **octal** PSRAM, which permanently
consumes GPIO 33–37 (GPIO 26–32 are already the SPI flash). Safe outputs are GPIO 1, 2, 4–18, 21, 39–42,
47, 48. Strapping pins 0/3/45/46 must not drive a shade contact — a reset glitch there is a shade command
on every boot and every OTA, which is exactly what `ha_dout`'s non-strapping requirement exists for.

On the C6 the strapping set is different — GPIO 4, 5, 8, 9, 15 — with GPIO 24–30 on flash, 12/13 on
USB-Serial-JTAG and 16/17 on UART0.

**`ha-hvac` is a Seeed XIAO ESP32-C6**, which breaks out D0–D10 only. Conveniently **none of those are
strapping pins**, so every header pin is safe to drive a contact from:

| Function | XIAO pin | GPIO |
|---|---|---|
| RS-485 TX (`UART_NUM_1`) | D10 | 18 |
| RS-485 RX (`UART_NUM_1`) | D9 | 20 |
| Aprilaire `DH` relay | D1 | 1 |
| Broan `OVR` failsafe relay | D2 | 2 |
| spare | D0, D3, D4, D5, D6, D7, D8 | 0, 21, 22, 23, 16, 17, 19 |

GPIO 15 (user LED), 9 (boot button) and 14/3 (antenna switch) are onboard, not on the header.

⛔ **AMENDED 2026-09-22 — RS-485 moved off D6/D7 to D10/D9, and pinned to `UART_NUM_1`.** This ADR
originally read *"Console/flashing use USB-Serial-JTAG (GPIO 12/13), so D6/D7 are free for the RS-485 UART
despite their TX/RX labels."* **That premise is false for this tree.** `edge/esp32c6/sdkconfig` sets
`CONFIG_ESP_CONSOLE_UART_DEFAULT=y` with `CONFIG_ESP_CONSOLE_UART_NUM=0` and USB-Serial-JTAG as merely
`CONFIG_ESP_CONSOLE_SECONDARY_*` — the primary console is UART0, i.e. GPIO16/17, i.e. D6/D7. A transceiver
landed there would emit the ROM-bootloader banner and the full IDF boot log at 115200 baud onto the ERV's
live bus at every reset, alongside a working wall control.

This is a **safety** amendment, not an ergonomic one. §1.12 bring-up rests on `LISTEN_ONLY` guaranteeing
zero bus writes, and both gates (`ha_rs485` transport, `ha_broan` protocol) are application-layer — they
cannot exist yet when the ROM bootloader is talking. D6/D7 would have made the headline guarantee of this
ADR unenforceable during the exact window that matters most.

The port pin matters as much as the GPIO pins: `uart_set_pin()` relocates the console along with UART0, so
configuring `ha_rs485` with `UART_NUM_0` re-creates the identical hazard on *any* GPIOs. Use `UART_NUM_1`.

D3/GPIO21 is no longer reserved for DE — the transceiver on hand (Waveshare TTL TO RS485 (C), already
named at `ha_rs485.h:24-26`) is auto-direction, so `de_gpio = -1`. The DE path stays supported in the
component for a future module that exposes the pin; it simply isn't wired on this build.

*Prompted by a wiring question from Hugh on 2026-09-22 — D10/D9 were proposed purely because they made the
harness easier to trace. The console conflict was found while checking whether the swap was safe.*

⚠️ **The XIAO C6 antenna switch is software-controlled** and defaults to the internal ceramic antenna. A
node in a mechanical room full of ductwork probably wants the U.FL external antenna — which requires the
firmware to drive **GPIO3 low and GPIO14 high**. Miss that and RF still goes to the ceramic antenna, which
presents as an inexplicable placement problem rather than a config error.

**The decomposition did not change to accommodate any of this**, which was the point. Had the split gone
the other way, both nodes would link the same unchanged modules — a column in
[MATRIX.md](../../edge/MATRIX.md), not a fork.

### 3. `ha_modbus` is not built

Nothing in this set speaks Modbus. Building it now would be speculative infrastructure justified by a
protocol assumption that already proved wrong once. When a genuine Modbus device arrives, `ha_rs485` is
the seam it hangs off.

### 4. Every write-only actuator gets an independent truth source

A dry contact is a *request*, not a confirmation. Each actuator pairs its control path with a physically
separate observation:

| Device | Control | Independent truth |
|---|---|---|
| Aprilaire E070 | relay on `DH`/`DH` | **metering plug on the 115 V cord** — idle < 3 W vs compressor ≈ 620 W |
| Broan ERV | RS-485 | the bus itself reports CFM, RPM, watts, faults |
| Gaposa shades | dry contacts | **none available** — see §5 |

The Aprilaire pairing is the load-bearing one: it detects **commanded on but drawing no power**, which is
precisely how a silent `E8` dew-point lockout or an `E7` float trip presents. The control path alone can
never see either.

This also preserves redundancy as a property rather than an aspiration — two independent paths beat one
richer path, which is why the relay route was chosen over the (mutually exclusive) RS-485 route that would
have given true run state but only one path.

### 5. Estimated position is stored as an estimate

The Gaposa link gives three reliable **absolute** states — fully open, fully closed, and the motor's
stored intermediate position, all motor-side limits that re-datum on every full run. Everything between is
dead reckoning from travel time and drifts with every command.

**Commanded position and estimated position are stored as distinct values with an explicit confidence /
staleness flag.** An estimate is never recorded as though it were a measurement. Re-datum to a limit
periodically.

### 6. The ERV keeps a failsafe that survives the node

The `OVR` dry contact (short `OVR` to `12V`) is a **hard override that beats the serial bus** and works
with our node dead. It is wired in parallel with the RS-485 takeover and left there permanently — one
relay against the possibility that an OTA leaves the ventilator in E50.

### 7. Nothing is written blind

Bring-up is staged for every device, and the ERV's `LISTEN_ONLY` mode is the model: sniff the real
wall-control conversation with the stock controller still attached, validating wiring, polarity, baud and
checksum at **zero bus writes and zero risk**, before ever taking the bus. Read-only active second. Writes
third, starting with a benign value.

Register `00 20` value `0x02` (OVR) is **never written** — it wedges the unit until cleared.

---

## Rejected alternatives

**`ha_modbus` as the ERV transport.** Rejected on evidence: the protocol is not Modbus. Recorded here
because the assumption survived several exchanges and would have produced a wrong abstraction at the
bottom of the stack.

**RF emulation of the Gaposa remote (CC1101 + SDR capture).** Rejected once the QCTZ36SDU was correctly
identified as a dry-contact controller. Retained as a documented fallback only. The research stands on its
own merits as a *reason not to go there*: undocumented framing, no existing decoder, and an unresolved
rolling-code risk that could have invalidated the whole approach after days of bench capture.

**RS-485 on the Aprilaire `A`/`B` Remote terminals.** Genuinely tempting — it reports true run state and
the unit's own RH, and the prior art is literally an ESP32-C6 with a MAX485. Rejected because it is
**mutually exclusive** with the relay path (External and Remote are different control sources), so it
trades two independent paths for one richer one. Also undocumented by the vendor, and the prior art does
not establish whether the transceiver ground may safely bond to the unit. Kept as a documented future
upgrade. *(2026-09-22: the ground-bonding objection is weaker than when written — the transceiver we
actually bought is galvanically isolated, so the question becomes moot rather than unanswered. The
mutual-exclusivity objection is the load-bearing one and is unchanged.)*

**Solid-state / optoMOS output for the Aprilaire.** Rejected by the vendor's own wording: *"normally open
(NO), dry contact (i.e. **not a triac or other semiconductor**) relay."* A triac cannot pass the DC sense
current and it leaks. This reverses the usual preference for solid-state and is worth recording so it is
not "optimized" back later.

**Broan `0–10 V` analog control.** Does not exist on this platform — the string "0-10" appears nowhere in
the 52-page manual.

**Broan Overture / any vendor cloud.** Cloud-only, no documented local API, no HA integration. Against the
prod-self-sufficiency north star.

**J13 `Vent` dry contact as the ERV's primary control.** Documented and functional, but it *"will override
the main wall control"* — it conflicts with the RS-485 takeover, and J13 carries 24 VAC adjacent to the
12 V J9 block we use. Recorded for completeness; not our path.

---

## Open decision — Gaposa linkIT vs. QCTZ36SDU

**Not resolved. Hugh's call; it does not block module work**, because `ha_gaposa` would swap `ha_dout` for
`ha_rs485` underneath without changing its interface — which is itself a demonstration of why the seams
are cut where they are.

| | QCTZ36SDU (owned) | linkIT-US24 |
|---|---|---|
| Node interface | **18 isolated outputs** | **1 UART**, documented 5-byte frame |
| Channels | 6 | 24 |
| Pairing | physical buttons only | **over the wire** (`0xAA`/`0xAB`) |
| ACK | none | 3-byte |
| Power | **120 VAC hard-wired inside our enclosure** | **5 V USB** |
| Conflict lockout | box-wide; mixed directions trip it | not documented as a constraint |
| Cost | ~$228–266 (already purchased) | ~$252–294 |

Two points carry real weight beyond convenience. Pairing over the wire removes the one operator dead-end
in the QCT path — the XS40 has **no motor-head program button**, so a lost remote otherwise means a human
at the motor. And 5 V USB power dissolves the unresolved question of whether the QCT's dry-contact inputs
are galvanically isolated from its 120 VAC supply, rather than requiring us to measure our way around it.

Neither option provides position feedback. That is a property of the motor.

---

## Consequences

**Good**

- One `ha_dout` carries three appliances; one `ha_rs485` is ready for the next serial device without
  presuming its protocol.
- The one-node-vs-two question is deferred at zero cost.
- The ERV gains a better humidity input than its own wall control ever had — it has **no humidity sensor**
  and expects the controller to supply RH, so we feed it a whole-house aggregate instead of one wall
  reading.
- Two of three actuators get independent verification of what they actually did.

**Accepted costs**

- **The ERV becomes dependent on our node.** After the wall-control swap its only local UI is the onboard
  LCD, and a node outage may fault it. Mitigated by the `OVR` failsafe, not eliminated.
- **The shades are open-loop.** Position is an estimate, flagged as such, with three absolute datums to
  re-anchor against.
- **`ha_gaposa` carries an unusual interlock.** The QCT trips box-wide if two channels are driven with
  *different* commands simultaneously, so mixed-direction scenes must be serialized in firmware. A "close
  everything except the office" scene is the natural way to hit this.
- **Three device profiles means three register/command maps to keep honest.** The design doc carries
  explicit CONFIRMED / LIKELY / UNKNOWN markers per fact; those markers are load-bearing and must not be
  promoted without a measurement.

**Blocking before install**

1. **QCT `com` → earth.** If the input section is mains-referenced, the node does not mount inside that
   enclosure and the output device choice changes.
2. **Broan E50 recovery behaviour.** Decides whether OTA is routine or requires a physical trip.
3. **Aprilaire `EXTERNAL` screen exists on the plain E070.** If absent, `DH` may be inert on this SKU and
   the decision in §4 has to be revisited.

Full ledger: [design doc §8](../design/hvac-shade-device-integration.md#8-open-questions-ledger).
