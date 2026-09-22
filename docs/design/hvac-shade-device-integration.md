# HVAC + shade actuator integration — protocol reference & build spec (implements ADR-0041)

**Date:** 2026-09-20  **Status:** Research complete, pending bench verification  **Owner:** dev.
Decision record: [ADR-0041](../adr/ADR-0041-hvac-shade-actuator-integration.md).
Covers four devices arriving late Sept / early Oct 2026: a Broan ERV, an Aprilaire dehumidifier, a
Gaposa shade controller, and a Gaposa tubular motor.

> **Provenance discipline.** Every protocol fact below carries a confidence marker:
> **CONFIRMED** (traced to a document or source file someone actually read), **LIKELY** (strong
> inference or single-operator bench report), **UNKNOWN** (not documented anywhere found).
> This distinction is the most valuable thing in this document — a wrong register address that we
> *write* can damage equipment, and a wrong assumption about terminal voltage can destroy a node or
> injure someone. **Nothing here has been verified on our own bench yet.** Do not promote a LIKELY
> to a CONFIRMED without a measurement.

---

## Scope

| Device | Model | Interface | Module |
|---|---|---|---|
| ERV | Broan `IAQ-AM1-1-B160E75RS` | RS-485, proprietary Broan framing | `ha_rs485` + `ha_broan` |
| Dehumidifier | Aprilaire `E070` | Dry contact (`DH`/`DH`) | `ha_dout` + `ha_aprilaire_dehum` |
| Shade controller | Gaposa `QCTZ36SDU` | 18× dry contact | `ha_dout` + `ha_gaposa` |
| Shade motor | Gaposa `XS40-AC6024` | 434.15 MHz, downstream of the controller | — (no direct interface) |

---

## 1. Broan ERV — `IAQ-AM1-1-B160E75RS`

### 1.1 Part number

**CONFIRMED.** There is no separate "IAQ-AM1" automation module — a working assumption that turned out
to be wrong and would have sent us looking for documentation that does not exist.

| Fragment | Meaning |
|---|---|
| `IAQ` | Distributor catalogue category ("Indoor Air Quality"). Not a product. |
| `AM1` | Broan's **platform/chassis code** for the AI Series. Appears in Broan's own spare-part names ("Core ERV 65% AM1", SV66113). The unit's Model register returns the string `AM1G4`. |
| `-1-` | Catalogue sub-index (inferred) |
| `B` | Broan brand (vs. `V` Venmar / vanEE / NuTone / Best — all Broan-NuTone, same chassis) |
| `160` | 160 CFM nominal (35–140 CFM delivered @ 0.2" w.g.) |
| `E` | **E**RV (latent + sensible). `H` = HRV |
| `75` | 75% sensible recovery @ 32 °F |
| `RS` | **R**ound **S**ide ports (6"). `RT` = round top |

So: a bare AI Series AM1-platform 160 CFM ERV with side ports. Control surface is the onboard LCD plus
the J9 terminal block.

### 1.2 It is not Modbus

**CONFIRMED.** The RS-485 port speaks a proprietary Broan framed protocol. Evidence:

- The official 52-page AI Series installer manual contains **zero** occurrences of "Modbus", "RS-485",
  "RS485", "baud", or "0-10" (full-text extraction of all 52 pages).
- The Home Assistant community thread spent years failing: *"all the combinations of Baud Rate / Data
  Bits / Stop Bits / Parity … led to the CRC failing"* — because there is no Modbus CRC.
- Broan **declined to share specifications** when asked.

**Wire format** (CONFIRMED, from `nspitko/broan_erv_uart` source):

```
[0x01] [target] [sender] [0x01] [len] [payload …] [checksum] [0x04]
```

- ERV address `0x10`; our client `0x12`. Protocol allows up to 32 addresses.
- `len` = payload byte count.
- Checksum is an **8-bit sum with deliberate overflow, not a CRC**:
  `0xFF & (0 - ((0x01 + sender + receiver + 0x01 + len + Σpayload) - 1))`

**Opcodes** (CONFIRMED):

| Opcode | Meaning |
|---|---|
| `0x02` / `0x03` | ERV Ping (payload ASCII `"Ping"`) / controller Pong |
| `0x04` / `0x05` | **Flow-control token** offer / ACK |
| `0x20` / `0x21` | Register read request / response (TLV: `hi lo len data…`) |
| `0x40` / `0x41` | Register write request / ACK |

**This is not a master/slave poll model.** The ERV offers the token with `0x04`; we take it with `0x05`,
send our batch, then return it. Max 10 registers per read request in the reference implementation.

### 1.3 Serial parameters

| Parameter | Value | Confidence | Source |
|---|---|---|---|
| Baud | **38400** | CONFIRMED | upstream ESPHome YAML |
| Framing | **8N1** | CONFIRMED | `__init__.py` `FINAL_VALIDATE_SCHEMA` enforces `parity="NONE", stop_bits=1` at compile time |
| ERV address | `0x10` | CONFIRMED | `broan.h` `m_nServerAddress` |
| Our address | `0x12` | CONFIRMED | `broan.h` `m_nClientAddress` |
| Duplex | Half, token-passed | CONFIRMED | `0x04`/`0x05` in `broan.cpp` |
| Heartbeat | write `40 00 50 00` every **10 s** | CONFIRMED | `runTasks()`, `HEARTBEAT_RATE 10000` |
| Control timeout | **5000 ms** without token → error | CONFIRMED | `broan.h` `CONTROL_TIMEOUT` |
| Termination | probably not needed | LIKELY | upstream: *"I found I did not need a termination resistor, but you may need one"* |

The old community guesses of 9600/19200 were simply wrong — that is why those attempts failed for years.

### 1.4 Register map

Two-byte opcode, written here in wire order as `HH LL`. Multi-byte values are 4-byte little-endian;
floats are IEEE-754 single.

**Tier A — read directly from upstream `components/broan/broan.h`. Confidence HIGH unless noted.**

| Addr | R/W | Type | Units | Meaning | Conf. |
|---|---|---|---|---|---|
| `00 20` | R/W | Byte | enum | **Fan mode** (commanded) | HIGH |
| `02 20` | R | Int32 | enum | Base mode — reverted to when turbo/override expires | HIGH |
| `07 20` | R | Int32 | enum | Active mode (0=Idle 1=Running 2=Max 3=Turbo 4=Manual) | MED |
| `0F 22` | R/W | Byte | 0/1 | Humidity control mode enable | HIGH |
| `02 22` | R/W | Int32 | seconds/hr | Intermittent period (off = remainder of hour) | HIGH |
| `0C 22` | R/W | Float | %RH | Target humidity A — write together with B | HIGH |
| `0A 22` | R/W | Float | %RH | Target humidity B — write together with A | HIGH |
| `14 00` | R | Int32 | s | Uptime | HIGH |
| `23 50` | R | Float | **W** | Power draw | HIGH |
| `01 E0` | R | Float | ° | Supply/intake air temp (*"will generally read high"*) | HIGH |
| `03 E0` | R | Float | ° | Exhaust air temp — NaN without the 2nd thermistor | HIGH |
| `05 10` | R | Float | CFM | Measured supply airflow | HIGH |
| `06 10` | R | Float | CFM | Measured exhaust airflow | HIGH |
| `03 10` | R | Float | RPM | Supply fan | HIGH |
| `04 10` | R | Float | RPM | Exhaust fan | HIGH |
| `06 22` | R/W | Float | CFM | **MED/manual target CFM — supply** (the speed setpoint) | HIGH |
| `08 22` | R/W | Float | CFM | MED/manual target CFM — exhaust | HIGH |
| `0E 50` / `0F 50` | R/W | Float | CFM | MAX target supply / exhaust | HIGH |
| `0A 50` / `0B 50` | R/W | Float | CFM | MIN target supply / exhaust | HIGH |
| `00 50` | W | Void | — | **Heartbeat** (every 10 s) | HIGH |
| `04 50` | W | Float | %RH | **Controller humidity — we must supply this** | HIGH |
| `05 50` | W | Float | ° | Controller temperature | MED |
| `01 30` | W | Byte | `0x01` | Filter reset | HIGH |
| `08 30` | R/W | Int32 | s remaining | Filter life (default 7 884 000 s = 91.25 d) | HIGH |
| `09 30` | W | Int32 | s | FilterLifeStage — **must be preloaded before `01 30`** or the reset is ignored | HIGH |
| `17 00` | R | Int32 | −1 = OK | **Fault code** → E-table | HIGH |
| `1A 00` | R | Int32 | −1 = OK | **Warning code** → W-table; cycles through multiple actives | HIGH |
| `02 60` | R | String | ASCII | Model (returns `"AM1G4"`) | HIGH |
| `02 00` | R | String | ASCII | Firmware name (e.g. `"am_main"`) | HIGH |
| `01 00` | R | String | 3 B decimal | Firmware version (e.g. 1.101.40) | HIGH |
| `01 60` | R | String | 3 B decimal | Hardware revision | HIGH |

**Fan mode enum (`00 20`)** — CONFIRMED from `enum BroanFanMode`, cross-checked against the manual's LCD table:

| Value | Name | LCD | Note |
|---|---|---|---|
| `0x01` | Off | `STB` | |
| `0x02` | Ovr | `OVR` | ⛔ **DO NOT WRITE** — hard override, wedges the unit until cleared. Read/display only |
| `0x06` | Recirculate | `REC` | Only on models with the recirculation damper (J6) — **not fitted on all models** |
| `0x08` | Intermittent | `INT` | |
| `0x09` | Min | — | |
| `0x0A` | Max | — | |
| `0x0B` | Manual | `MED` | Variable; pairs with `06 22`/`08 22` |
| `0x0C` | Turbo | `TUR` | |
| `0x0D` | Humidity | `HUM` | |
| `0x0F` | Away | `OTH` | |
| `0x11` | Smart | `SMT` | |
| — | — | `AUT` | **UNKNOWN** — no confirmed register value |
| — | — | `DEF` | **UNKNOWN** — defrost has no mapped register |

**Fan speed % is derived, not a register.** `setFanSpeed(0-100)` linearly remaps onto
`[CFMIn_Min, CFMIn_Max]` and writes the result to *both* `06 22` and `08 22`.

**Tier B — bench-validated by one contributor, NOT merged upstream. Treat as read-only leads.**

| Addr | Type | Meaning | Note |
|---|---|---|---|
| `01 22` | Int32 | Override duration, minutes | |
| `07 E0` | Float | PCBA / electronics temperature (ties to E42/E43/W61) | |
| `08 E0` | Float | "Airstream humidity" | **CONTESTED — see below** |
| `09 E0` | Float | Airstream humidity (2nd value) | **CONTESTED** |
| `08 20` | Byte | Damper/mode flag; reads 0 on AM1G4 | ⛔ do not write |

**Tier C — observed but unidentified. ⛔ Do not write any of these.**
`02 30` · `0E 21`…`04 21` · `00 30` · `00 22` · `07 50` · `03 20` · `08 20`

### 1.5 The ERV has no humidity sensor

**CONFIRMED, and architecturally significant.** Upstream README states flatly: *"The ERV does not have a
humidity sensor — the current humidity reading is sent periodically from the controller."*

Broan's own documentation corroborates: the AI Series wiring diagram and the 20-item service-parts list
show **only thermistors** — `RT1 (NTC)` at J7a, "Thermistor SV66134", "Separator WE with thermistor
SV66149", "Recirculation damper with thermistor SV66148". Error codes E40 (outside air thermistor, J7A)
and E41 (distribution air thermistor, J7B) confirm exactly **two** air thermistors. No humidity sensor
appears anywhere.

**Consequence:** we feed RH in via `04 50`. This is an *upgrade* over the stock wall control, which
reports RH from one wall location — we can supply a whole-house aggregate from the existing sensor fleet.

Registers `08 E0`/`09 E0` do return changing values on some units, which upstream flags as suspicious.
**Log them, correlate against a known-good RH source, but architect assuming we must supply RH.**

### 1.6 Fault (E) and warning (W) codes

Authoritative decode for `17 00` and `1A 00`, from the official manual §8 (pp. 21–23).

**Faults:** E01 supply damper range · E02 supply damper timeout · E03 supply damper · E05 exhaust damper
range · E06 exhaust damper timeout · E07 exhaust damper · E09 recirc damper range · E10 recirc damper
timeout · E11 recirc damper · E22 supply airflow · E23 supply motor over-current · E24 over-voltage ·
E25 under-voltage · E26 over-temp · E27 FOC duration · E28 speed feedback · E29 startup · E32 exhaust
airflow · E33–E39 exhaust motor (same series) · E40 outside air thermistor · E41 distribution air
thermistor · E42 PCBA thermistor · E43 PCBA temp over limit · **E50 wall control communication lost** ·
E51 wall control sensor · E60 protection mode

**Warnings:** W22 supply airflow · W32 exhaust airflow · W40 outside air thermistor (*"unit still in
operation, but preventive defrost cycles are added"*) · W52 initial setting incomplete · **W61 protection
mode / electronics overheating** (*"power to the motor is deliberately reduced… should appear only when
set to high speed in a warmer environment, e.g. over 30 °C (86 °F)"*)

**W61 deserves monitoring** — it means the unit is silently derating airflow, which otherwise presents as
an unexplained CFM drop.

### 1.7 Wiring — J9

**CONFIRMED** from the manual wiring diagram (p. 15). Broan part **SV66145**, a 6-position green screw
terminal block. Not RJ45.

| Terminal | Function | Our use |
|---|---|---|
| `OVR` | Override input (aux wall control / dry contact) | **Fallback path — §1.9** |
| `LED` | LED status return to aux control | unused |
| `12V` | 12 VDC isolated, PTC-protected | ⛔ see hazard 2 |
| `D-` | RS-485 differential − | → adapter **A** |
| `D+` | RS-485 differential + | → adapter **B** |
| `GND` | Isolated ground | → adapter GND |

**Polarity is counterintuitive: `D+` → `B`, `D-` → `A`.** Upstream: *"Somewhat confusingly, A is D- and B
is D+."* Getting it backwards is non-destructive — it presents as `"Alignment: Unexpected XX"` log spew.

#### ⚠️ Hazards

1. **J9 vs J13.** J9 carries 12 VDC. The **separate J13 block** (`Vent, Y, W, C, Gf, G, R`) carries
   **24 VAC** for the furnace interlock. Manual p. 13, verbatim: *"A miswiring that sends a 24 VAC signal
   to the 6-position terminal block (OVR, LED, 12V, D-, D+, GND) could permanently damage the control
   circuit. Verify carefully wire connections before powering-up the unit."* **Meter J9 before landing
   anything. Never cross J13 and J9.**
2. **Do not power the node from J9's 12 V.** It destroys a 3.3 V board, and the available current is
   **UNKNOWN** (manual shows only PTC protection). Power the node separately; run J9 `GND` to node GND
   only to bond references — and only if using a non-isolated transceiver.
3. **D+/D− are differential, not TTL.** The transceiver is mandatory.
4. Keep the run away from contactors, dimmers, motors, panels. Twisted pair.

### 1.8 ⛔ The E50 availability hazard

**This is the most important operational finding for the ERV, and it must be designed for before cutover.**

Only **one** controller may exist on that bus. Manual §3.1.1: *"Never install more than one optional main
wall control per unit."* Upstream: *"This library does not coexist with other serial wall remotes. This is
a software limitation on the ERV itself, it will only ever respond to one device on the bus."*

So we **replace** the wall control, and thereby inherit its liveness obligation. Heartbeat every 10 s,
5 s token timeout. Drop off the bus and the ERV raises **E50 "wall control communication lost" and shuts
down.**

Our node will drop off that bus routinely: **every OTA, every crash, every power blip.**

**UNKNOWN and blocking: does E50 self-clear when the controller returns, or does it require a manual power
cycle?** If the latter, every firmware update means a physical trip to the unit — an operator dead-end
behind a routine operation. **Determine this on the bench before the wall control comes off permanently.**

After the swap the ERV's only local UI is its onboard LCD.

### 1.9 Fallback — OVR dry contact

**CONFIRMED**, manual §3.2.2: *"Electrical connection to dry contact optional auxiliary wall control (e.g.
crank timer)"* — **short `OVR` to `12V` through any dry contact.** That is the entire wiring. The ERV
reports `OVR CNT` on the LCD and `0x02` on register `00 20`.

Behaviour is selectable on the LCD under **OVR** (verbatim, §3.2.1):
- **`BAL`** — *"the unit remains balanced while providing maximum airflow"*
- **`PER`** — *"slightly unbalanced since the distribution motor is in MAX speed while allowing maximum
  exhaust ventilation"*
- **`DIS`** — *"unbalanced since air distribution is constant despite a higher need in exhaust ventilation"*

This is a **hard override that beats the serial bus** — which is exactly why it is a good failsafe, and why
the reference implementation makes `ovr` display-only in its mode select. **Wire it in parallel and leave
it there.** It survives the node being dead.

### 1.10 Second fallback — J13 `Vent` (documented, but conflicts)

Manual §3.3.1. Closing `Vent` toggles the unit out of Standby into an LCD-selected mode
(minimum / intermittent / auto / maximum). Its `auto` sub-mode runs an outdoor-temperature schedule:

> < −13 °F = 10 min/hr · −13→19 °F = 20 min/hr · 19→50 °F = 40 min/hr · 50→77 °F = MIN speed ·
> 77→82 °F = 30 min/hr · 82→91 °F = 20 min/hr · > 91 °F = 10 min/hr

⚠️ *"This dry contact option will override the main wall control so we do not recommend the use of a wall
control with this type of connection"* — **it conflicts with our RS-485 takeover.** Also J13 carries
24 VAC. Documented for completeness; not our path.

### 1.11 Ruled out

- **0–10 V analog:** does not exist. The string "0-10" appears nowhere in the 52-page manual.
- **Broan Overture** (BIAQRS100 etc.): a separate add-on product, **not in our SKU**, and **cloud-based**
  with no documented local API and no HA integration. Against the prod-self-sufficiency north star.
  Ignore.

### 1.12 Bring-up sequence — never write blind

1. **LISTEN_ONLY, wall control still attached.** Upstream ships a `LISTEN_ONLY` compile flag (`broan.h`
   line 40). This passively sniffs the real wall-control conversation and validates wiring, polarity, baud
   and checksum with **zero bus writes and zero risk**. Highest-value first step; costs nothing.
2. **Read-only active.** Disconnect the wall control, let us take the bus, all writes commented out.
   Confirm `02 60` returns a model string and temps/CFM/watts look sane. Log Tier C via
   `-DSCAN_UNKNOWN=1` — note this scanner was **broken until PR #15**, so verify it actually emits before
   trusting its silence.
3. **Then write**, starting with `00 20` → `0x09` (Min). **Never `0x02`.**

---

## 2. Aprilaire E070 dehumidifier

### 2.1 Identity

**CONFIRMED.** The E070 is the renamed **Model 1820** (changed May 2021) — *not* a successor to the
1830/1850, which became the E080/E100. It shares the 1830/1850/1870 control board and terminal block,
which is why that generation's documentation applies.

70 pint/day, up to 2 200 sq ft, 115 VAC, 5.4 A, **< 3 W idle**.

### 2.2 Terminal block

| Terminal | Group | Function | Type | Conf. |
|---|---|---|---|---|
| **`DH`** ×2 | — | **External control input** | **Dry contact**, self-contained pair. V and I **not stated in any manual** | CONFIRMED (function) / **UNKNOWN** (V, I) |
| `FLOAT Switch` ×2 | FLOAT | Condensate overflow cutoff | Dry contact, **normally closed, ships jumpered**. Open ⇒ fault `E7`, compressor stops | CONFIRMED |
| `+` / `−` | Remote | Model 76 supply | **9 VDC** from the dehumidifier board | LIKELY |
| `A` / `B` | Remote | RS-485 data, 9600 8N1, proprietary | reverse-engineered | LIKELY |
| `ODT` ×2 | Sensor | Outdoor temp sensor (P/N 8052) | NTC thermistor, 26 kΩ @ 40 °F, 9.5 kΩ @ 80 °F | CONFIRMED |
| 4 unlabeled | — | `VENT`/`DEH` damper outputs on E080/E100 | unlabeled in E070 figures | **UNKNOWN for E070** |
| `Gh` | HVAC EQUIP | G to air handler — blower interlock **output** | switched 24 VAC from `Rf` | LIKELY (fn) / UNKNOWN (type) |
| `Rf` | HVAC EQUIP | R from furnace — 24 VAC hot in, **40 VA min** | 24 VAC input | CONFIRMED |
| `Cf` | HVAC EQUIP | C from furnace — 24 VAC common | 24 VAC input | CONFIRMED |
| `Gs` | HVAC EQUIP | G from thermostat | 24 VAC input | LIKELY |
| `W` | HVAC EQUIP | Heat-call sense (optional) | 24 VAC input | CONFIRMED |
| `Y` | HVAC EQUIP | Cool-call sense — inhibits compressor during AC | 24 VAC input | CONFIRMED |
| `NC \| NO` slide switch | on board, beside DH | Selects how `DH` is interpreted | CONFIRMED on E080/E100 + 1830/1850/1870; **presence on E070 UNVERIFIED** |

HVAC block order as printed: `Gh, Rf, Cf, Gs, W, Y`.

⚠️ **Generation hazard:** on the *older* 1710A/1750A/1770A the external-control wires go to **`DH` and
`Rf`** — 24 VAC is in that loop. On the 1830/1850/1870/E-series the pair is **`DH`–`DH` and
self-contained**. Do not apply older wiring guidance to this unit.

### 2.3 Must be an electromechanical relay

**CONFIRMED, and it overrides the usual "prefer solid-state" instinct.** Aprilaire specifies a
*"normally open (NO), dry contact (i.e. **not a triac or other semiconductor**) relay to complete the
circuit between the DH terminals."*

A triac cannot pass the DC sense current these inputs likely use, and it leaks. **Verify the module has a
physical relay can that clicks, not an SSR.**

### 2.4 Wiring

```
        ESP32 side (SELV, 3.3 V)         │ ISOLATION BARRIER │   Dehumidifier side
                                          │ (relay coil-to-   │
                                          │  contact gap)     │
  ┌──────────────┐                        │                   │
  │   ESP32      │                        │                   │
  │  GPIO ───────┼────────► IN ─┐         │                   │
  │  3V3 ────────┼────────► VCC │ relay   │                   │
  │  GND ────────┼────────► GND │ module  │                   │
  └──────────────┘              │         │                   │
                          coil ─┴──[ Q + flyback D ]           │
                                     │                         │
                              ┌──────┴──────┐                  │
                              │   RELAY     │  COM ────────────┼──► DH (either leg)
                              │   CONTACT   │   NO ────────────┼──► DH (other leg)
                              └─────────────┘                  │
   NOTHING crosses this line ──────────────────────────────────┘
   No shared ground. No wire from ESP32 to the dehumidifier.
```

Rules:

1. **The relay contact is the only connection.** Do not bond node ground to the E070. The contact pair is
   floating and non-polar — orientation of the two `DH` wires does not matter.
2. **Use the NO contact**, and set the board's `NC|NO` switch to **`NO`**, so an unpowered or crashed node
   leaves the unit not calling.
3. **The GPIO cannot drive a relay coil directly.** 3.3 V coils draw **70–120 mA**; ESP32 GPIO is ~20 mA
   nominal / 40 mA absolute max. The module's driver transistor + flyback diode carries it; the GPIO drives
   only the opto LED / base at a few mA. **Confirm the module has both.**
4. **Opto-isolation on the module input is not the safety barrier** — the relay's coil-to-contact gap is
   (typically 1500–4000 VAC). If using the opto, power the coil from a separate rail or the shared supply
   bypasses it.
5. **Coil inrush can brown out the node** if both share a small regulator. Give the module headroom.
6. **Boot glitch:** pick a **non-strapping GPIO** and pull it to the inactive state, so the unit gets no
   spurious call at power-up or during OTA.

### 2.5 Required unit configuration — without this, the relay does nothing

The unit ignores `DH` in its default state.

1. **Leave the `FLOAT Switch` jumper installed** (unless a real float switch is fitted). Removing it gives
   fault `E7` and the compressor never runs.
2. **Set `NC|NO` to `NO`.**
3. **Installer setup:** unit **OFF** → hold **`MODE` 3 s** → page with `MODE` to the **`EXTERNAL`**
   screen → set **`ENABLED`** with ▲/▼ → keep pressing `MODE` through all remaining screens to exit
   (`DONE` blinks).
4. **Turn the unit ON.** Display should read **`EXTERNAL`**. External mode does *not* override the front
   panel — if the unit is OFF, nothing runs.
5. In External mode the unit *"doesn't use on-board sensors and uses DH/DH terminals to control the
   dehumidification on and off"*, and the normal **3-minute Air Sampling delay is bypassed**.

⚠️ **Needs bench verification.** The E070's *own* manual documents only `REMOTE`, `VENT/AIR CYCLING` and
`RH OFFSET` screens — it never mentions External control, the `DH` terminals' purpose, or the `NC|NO`
switch. It does say *"Not all system set-up options will be covered in these instructions"*, its terminal
block physically carries `DH`/`DH`, and it lists *"a separate, remote control such as a dehumidistat"* as a
reason to wire. The same-chassis E070W and same-platform E080/E100/E130 document External-via-DH
explicitly. **LIKELY-to-certain but not proven for the plain E070 — walk the menu and confirm.**

**Protections still active in External mode** (the relay is a *request*, not a command): compressor
anti-short-cycle, defrost, inlet air **50–104 °F with dew point ≥ 40 °F** (else fault `E8`), and
`DEH W/AC` may inhibit the compressor during a cooling call if `Y` is wired.

### 2.6 ⚠️ The 40% RH target collides with the E8 dew-point lockout

**Computed, worth designing around.** The unit refuses to run below a **40 °F inlet dew point**.

| Space temp | RH | Dew point | Margin to E8 |
|---|---|---|---|
| 70 °F | 40% | ≈ 44.5 °F | 4.5 °F |
| 65 °F | 40% | ≈ 40.0 °F | **0 °F — at the threshold** |

In a cooler basement or crawlspace the unit will fault out *while trying to reach the target*, and it will
present as a broken dehumidifier rather than a designed protection. Factor this into setpoint logic:
the achievable RH floor is a function of space temperature, not a constant.

This is also the strongest argument for the metering feedback below — an `E8` lockout is **silent except
on the LCD**.

### 2.7 Feedback — no status contact exists

**CONFIRMED.** No manual documents any status, fault, or alarm output. Faults `E1`–`E9` surface **only on
the LCD**.

| Rank | Method | What you get | Fidelity |
|---|---|---|---|
| 1 | **RS-485 on `A`/`B`** (replace Model 76) | True run state (`!`/`?`), unit's own RH, **write** control of power + dryness 1–7 | Highest — ground truth from the unit's firmware |
| **2** | **Metering plug / clamp CT on the 115 V cord** | Idle **< 3 W**, fan/sampling, compressor **≈ 5.4 A ≈ 620 W** | High — and **independent of the control path** |
| 3 | Current-sensing relay on the cord | Binary running / not | Medium — loses fan-vs-compressor |
| 4 | Tap `Gh` blower-interlock output | Blower call (incl. sampling), not compressor | Medium, conditional on `Rf`/`Cf` being fed |
| 5 | Optical tap of the LCD | Indirect | Low — backlight sleeps. Not recommended |
| — | Dedicated status contact | — | **Does not exist** |

**Option 1 and the relay path are mutually exclusive** — External (relay) and Remote (RS-485) are
different control sources and cannot coexist.

**Chosen: relay + metering plug (2).** It is the only combination that yields *two independent paths*, so
we can detect **commanded on but drawing no power** — exactly how a silent `E8` dew-point lockout or an
`E7` float trip presents. The control path alone can never see that.

### 2.8 The RS-485 alternative (documented, not chosen)

Prior art is directly on our platform: `dwrice0/aprilaire_controller`, an **ESP32-C6 + MAX485** replacing
the Model 76 on the E070's `A`/`B` terminals.

- RS-485, **9600 8N1**, ASCII-hex frames between `STX (0x02)` and `ETX (0x03)`, checksum = sum mod 256.
- **M-frame** (E070 → controller, ~1 Hz): `STX 'M' cmd rh[2] '0''0' cs[2] ETX` — `cmd` `?` = idle,
  **`!` = running**; `rh` = RH% in 2 hex chars.
- **R-frame** (controller → E070, within ~2 ms): `STX 'R' on[2] dryness[2] rh[4] sep[2] temp[2] cs[2] ETX`
  — `on` = 01/00, `dryness` = setpoint 1–7.

Kept as a documented future upgrade. If pursued: the prior-art repo does **not** document whether the
MAX485 ground must bond to the E070 `−` terminal, nor whether that node is genuinely SELV — a real gap.
Prefer an **isolated** transceiver to keep the galvanic separation the relay gives for free.

### 2.9 Not on Aprilaire's documented automation bus

Three things are commonly conflated:

1. **Aprilaire/Enerzone "STATNET" (8870/8800-series)** — a real documented ASCII command set over
   RS-232/485 at 9600 baud via the Model 8811 Protocol Adapter. It is a **thermostat** bus. **The E070 is
   not on it.** An 8-series thermostat commands the E070 the same way we will: by closing `DH`/`DH`.
2. **The E070's `+ − A B` Remote link** — separate, proprietary, vendor-undocumented (§2.8).
3. **`pyaprilaire` / the official HA integration** — 8800-series thermostats and 6000-series zone controls
   only. **Not applicable.** `ha-aprilaire-cloud` targets cloud-connected `W` models; the plain E070 has no
   radio.

---

## 3. Gaposa QCTZ36SDU shade controller

### 3.1 Identity — 6 channels, not 36

**CONFIRMED** from the Gaposa USA catalogue p. 13, under *"Gaposa 3rd party integration via dry contacts
with: Lutron, Savant, Control4, Crestron"*:

> **QCTZ3SDU  QCTZ36SDU** — "Panels with integrated transmitter enables to interface a radio motors with a
> home automation system. In this way, the home automation system will control the radio motor(s) through
> the UP/STOP/DOWN signals." — 1 channel / 6 channels.

| Fragment | Meaning |
|---|---|
| `QCT` | Gaposa transmitter family |
| `Z` | Emitto Smart Line generation |
| `3` | dry-contact control-unit **series** digit |
| `6` | **6 channels** (absent = 1 channel: `QCTZ3SDU`) |
| `SD` | dry contacts ("senza potenziale") |
| `U` | USA — 120 V~ 60 Hz |

**The "36" is series-3, 6-channel.** Governing manual: Gaposa doc `QCT3SD_QCT36SD_I_ML_0614`, shipped by
the distributor as the QCTZ36SDU instructions.

| Spec | Value | Source |
|---|---|---|
| Supply | **120 V~ 60 Hz ±10 %**, hard-wired, **cord not included** | manual + label |
| Fuse | 315 mA | manual |
| RF | 434 MHz integrated multi-channel transmitter, ~100 ft | manual + label |
| IP | IP44 | catalogue |
| Enclosure | 8.94″ × 7.63″ × 3.38″, ~2.1 lb, 6 grommeted cable ports | distributor |

**Unit in hand, read off the board 2026-09-20 (photos in `~/uploads/qct/` on ha-dev):**

| Field | Value |
|---|---|
| Label part | `QCTZ6SDU - E616USASC` |
| Label description | "Control unit dry contacts — **6ch** 434 MHz 120V~60Hz" |
| Firmware | **Firm. 13 soft. USA**, lot 0326 |
| PCB | `G2016SMC REV.01`, made in Italy |
| MCU | Atmel QFP44 |

The label's explicit "6ch" **CONFIRMS** the part-number decode in the table above — the `3` is a series
digit and the `6` is the channel count. This is not a 36-channel unit.

**It needs its own 120 VAC feed and offers no low-voltage auxiliary output. Do not tap it for node power.**

### 3.2 Terminals

**CONFIRMED**, manual p. 7. Six identical groups:

```
CH1   CH2   CH3   CH4   CH5   CH6
Up    Up    Up    Up    Up    Up
St    St    St    St    St    St
Dw    Dw    Dw    Dw    Dw    Dw
com   com   com   com   com   com
```

Plus on-board `SEL`, `Prog`, `Prog Canal`, `TX`, `FC`, `Sync`, `Limit`, an `ON` LED and per-channel LEDs
1–6. **No channel-select input and no DIP switch in the signal path** — all six channels are wired out in
parallel. `SEL` selects a channel **for programming only**.

**Relay budget: 18** (6 × Up/St/Dw).

### 3.3 Command semantics

**CONFIRMED**, manual p. 8, verbatim:

> 1. To activate an UP command make a momentary closure between **UP and Com**
> 2. To activate a DOWN command make a momentary closure between **DW and Com**
> 3. To activate a STOP command make a momentary closure between **ST and Com**
> 4. "Transmittion can be maintained for a maximum of **30 seconds**. In case the dry contac tinputs are
>    maintained for a longer time the board will stop transmitting and display an error message:
>    **all leds ON**."

**Momentary / edge-triggered.** A brief closure issues the command; the motor then runs to its stored limit
on its own. Releasing the contact does **not** stop the shade — `ST` is a separate command.

- **Minimum pulse width: UNKNOWN.** Not stated anywhere. Sweep from ~100 ms; 250–500 ms is a starting
  guess, explicitly a guess.
- **Maximum: 30 s, CONFIRMED.** A stuck relay does not merely run a shade — **it disables the box.**

### 3.4 ⛔ Two hard interlocks

**Per-channel:** never assert Up/Dw/St together on one channel. (Not explicitly documented for the same
channel, but plainly covered by the mixed-signal rule — LIKELY error. Interlock regardless.)

**Box-wide, and this one is easy to miss** — CONFIRMED, manual pp. 8–9:

> "In order to send two or more command signals at the same time it is important to make sure that the
> signals **are the same**. For example UP, DOWN or STOP signals cannot be mixed or QCT36SD will stop
> transmitting and display an error message: all leds ON."

The manual illustrates it **across channels**: CH1 UP + CH4 UP = correct; **CH3 DOWN + CH4 UP = error, all
LEDs on, transmission stops.**

**Firmware consequences:**

1. Per-channel Up/Dw/St interlock.
2. **Serialize mixed-direction commands across channels.** A scene like "close all shades except the
   office" — DOWN on CH1–5 while UP on CH6 — **bricks the box until the error clears.** Queue into
   separate time slots.
3. Same-command fan-out **is** allowed and is the efficient path for "all up" / "all down".
4. Hard pulse cap well under 30 s, enforced by a watchdog **independent of the command path**.

**UNKNOWN and blocking: how the "all LEDs ON" error clears** — release, or power cycle? Must be
determined on the bench so the recovery path lives in firmware rather than in a phone call.

### 3.5 Contact type — RESOLVED by inspection 2026-09-20

**Every dry-contact input is opto-isolated by its own PC817A.** Eighteen of them (6 channels × Up/St/Dw),
read directly off the board — marking `817A4 / V148 / 25`. Photos in `~/uploads/qct/` on ha-dev.

This closes what was the one genuinely dangerous unknown here. Consequences:

- **The inputs are galvanically isolated from the board logic and from the mains side.** Mounting our node
  inside the enclosure is electrically fine. This was previously gating that decision; it no longer is.
- **The input presents an LED, not a switch contact.** The board current-limits it; a "dry contact" closure
  simply completes the LED circuit. Typical PC817 drive is ~5–20 mA.
- **Polarity therefore matters** for any semiconductor output. A mechanical relay contact is
  polarity-agnostic and works regardless.

#### The relay may be unnecessary

Because the input *is* an optocoupler, a relay shorting that loop duplicates what the PC817 is already
there to do. Driving the LEDs directly from the node — 18 channels off two MCP23017 expanders — is
simpler, silent, has no contact wear, and keeps the isolation (the PC817 still separates our node from the
QCT's MCU; we just sit on its input side instead of a contact).

It also avoids a real mismatch in the relay path: **~5–20 mA is well under the minimum recommended contact
load for a 10 A power relay.** Contacts specified for switching amps can build an oxide film when they
never carry meaningful current — the dry-circuit problem. They would work; they are not the durable
choice.

#### MEASURED 2026-09-21 — drive method settled

| Measurement | Result | Consequence |
|---|---|---|
| All six `com` terminals | **shorted together** | one ground wire serves all 18 channels |
| `Up`→`com`, powered, open circuit | **16.55 V** | `com` is the rail NEGATIVE; low-side switching is correct |
| Series resistors | present, in series with each LED | board sets the LED current |

**Direct GPIO drive is OUT.** 16.55 V on a 3.3 V pin destroys it. The reading also disproves the earlier
"floating LED pair" reading — the opto input side does connect to a board rail; that was a tracing
artefact. ~16.5 V reads as an unregulated rectified secondary (a 12 VAC winding lands near 17 V unloaded)
and will sag under load.

Implied LED current, working back through a ~1.2 V forward drop: 1 kΩ → ~15 mA, 1.5 kΩ → ~10 mA,
2.2 kΩ → ~7 mA. All healthy for a PC817A.

**Per-channel interface — NPN low-side switch:**

```
  S3 GPIO ──[ 1 kΩ ]──┬── base
                      │
              2N3904  │ collector ── QCT `Up` / `St` / `Dw`
                      │
                      └── emitter ─── QCT `com` ──┬── S3 GND
                                                  └── (all six COMs already shorted)
```

Margins are large: 40 V Vceo against 16.55 V, ~15 mA against a 200 mA rating, ~3 mW dissipation, and
2.6 mA of base drive for a required gain under 6. Any small-signal NPN works.

#### Build spec — 3 × ULN2803A, two shade channels per chip

The per-channel discrete NPN is implemented as three **ULN2803A** Darlington arrays (8 channels each,
18 used, 6 spare). The ~1 V Darlington saturation drop costs ~7% of LED current on a 16.55 V rail —
negligible. (It would NOT have been negligible on a 5 V rail; the measurement is what made this part
viable.)

⚠️ **The outputs run in reverse.** Input pin *N* pairs with output pin *19−N* — IN1 (pin 1) drives OUT1
(pin **18**), directly across the package. Wiring it left-to-right mismatches every channel. This is the
classic error with this part.

```
  IN1  1 ┤●        ├ 18  OUT1
  IN2  2 ┤         ├ 17  OUT2
  IN3  3 ┤         ├ 16  OUT3
  IN4  4 ┤ ULN2803 ├ 15  OUT4
  IN5  5 ┤         ├ 14  OUT5
  IN6  6 ┤         ├ 13  OUT6
  IN7  7 ┤         ├ 12  OUT7
  IN8  8 ┤         ├ 11  OUT8
  GND  9 ┤         ├ 10  COM  (leave unconnected — flyback common, no inductive load here)
```

Allocated **two shade channels per chip** rather than packing 8-6-4, so a wiring error stays local and a
chip can be swapped without re-landing unrelated channels:

| Chip | Shade | Function | S3 GPIO | IN pin | OUT pin |
|---|---|---|---|---|---|
| U1 | CH1 | Up / St / Dw | 1 / 2 / 4 | 1 / 2 / 3 | 18 / 17 / 16 |
| U1 | CH2 | Up / St / Dw | 5 / 6 / 7 | 4 / 5 / 6 | 15 / 14 / 13 |
| U2 | CH3 | Up / St / Dw | 8 / 9 / 10 | 1 / 2 / 3 | 18 / 17 / 16 |
| U2 | CH4 | Up / St / Dw | 11 / 12 / 13 | 4 / 5 / 6 | 15 / 14 / 13 |
| U3 | CH5 | Up / St / Dw | 14 / 15 / 16 | 1 / 2 / 3 | 18 / 17 / 16 |
| U3 | CH6 | Up / St / Dw | 17 / 18 / 21 | 4 / 5 / 6 | 15 / 14 / 13 |

Pin 9 on all three chips ties to the common node: QCT `com` + S3 GND + wall-wart negative.

**It is a sinking, inverting driver** — input HIGH pulls the output down to `com`, which is the low-side
switch we want. GPIO HIGH = command asserted, so `ha_dout` is configured `active_high = true`. No polarity
surprise.

**3.3 V drive is fine** despite the 2.7 kΩ input resistor being nominally specified for 5 V logic: 3.3 V
gives ~0.7 mA of base current into a Darlington with gain over 1000. We need 15 mA out.

Build notes: use **DIP-18 sockets** so a dead chip swaps without desoldering 18 legs, and put a 0.1 µF
decoupling cap across pin 9 and the 5 V rail at each chip — not required for LED loads, but free, and
this is 18 switched lines sharing an enclosure with a 434 MHz transmitter.

**Powering the node — BUILT 2026-09-21:** a separate 5 V wall-wart supply is wire-nutted in parallel off
the QCT's F/N feed. One mains feed, one enclosure, and **zero load on the QCT transformer** — which
retires the VA question entirely. The wall wart's output floats (isolated SMPS), so **its negative must
tie to `com`**; that node is also S3 GND and the return for all 18 NPN emitters. Without that tie nothing
switches.

(The alternative considered and not taken: a buck module off the 16.55 V rail. Same topology, but it puts
the S3's ~70 mA on a transformer sized for an ATmega and the RF stage.) **Do NOT power the S3 from the board's regulated 5 V rail**: that rail is sized for an
ATmega plus the RF transmitter, and its ground is on the MCU side of the optos — referencing it while
driving LEDs against `com` would bridge the two domains.

⚠️ **Earthing:** the board has F and N only and no earth terminal — the signature of a **Class II /
double-insulated** design. There is nothing to bond earth to, and adding one is not a sanctioned
modification. The secondary is transformer-isolated, so an earth bond there provides no breaker fault path
regardless. If better fault protection is wanted, a **GFCI/RCD upstream** is the correct answer — it works
without an equipment ground, which is precisely how Class II equipment is meant to be protected.

Neither outcome changes `ha_gaposa`: it emits per-channel assertions and `ha_dout` applies them, whether
that lands on a relay coil or an expander pin. That is what the capability seam bought.

### 3.6 Pairing

**CONFIRMED**, manual p. 8:

> "Select the channel (1-6) you would like to program with the **SEL** button. Press **ProgTx** on the
> motor's master remote and press **UP or DOWN on QCT36SD** according to the shade's movement."

Each of the 6 channels carries its own transmitter identity and enrolls like any other Gaposa transmitter:

1. Press and **hold SYNC** on a remote **already paired** to that motor until the motor starts moving.
2. Note the rotation direction; release SYNC (motor stops).
3. **Within 5 seconds**, on the QCTZ36SDU: select the channel with `SEL`, then press the matching direction
   button — `UP` if the motor turned upward, `DOWN` if downward.

⚠️ **The XS40 has no program button on the motor head.** Every other Gaposa motor offers a motor-head
pairing route; the XS40 does not. **Pairing therefore requires an existing paired remote.** If that remote
is lost, the fallback is a power-cycle reset (power OFF→ON, then within 8 s hold SYNC+STOP on any Gaposa
transmitter until a long jog), after which limits survive but **every** transmitter must be re-enrolled.

**The dry contacts cannot be used for pairing** — it needs the panel's physical `SEL`/`UP`/`DOWN` buttons.
A one-time human step at install, but it means the node **cannot self-enroll or recover a lost pairing**.

Other procedures, all remote-button-only:
- **Reverse direction:** hold SYNC until motor moves, press STOP → jog. **Must be done *before* limit setting.**
- **Set limits (UP first):** hold LIMIT until jog → dead-man UP to top → STOP records → dead-man DOWN to
  bottom → STOP records.
- **Intermediate position:** park the shade, press UP+DOWN together until jog.
- **Delete this transmitter:** hold SYNC+STOP until brief jog.
- **Erase all transmitters:** hold SYNC+STOP ≥ 15 s. **Limits are not erased.**

### 3.7 Feedback — there is none

**Definitive, from three independent directions:**

- The terminal list contains **no output contacts**. Only the `ON` LED and per-channel LEDs. An optical tap
  would tell us the panel *transmitted*, not that the shade moved.
- **The RF link is one-way.** *"The remote controls are generally considered to be dummy devices as they do
  not store any motor or programming information."* And: *"The motor does not provide feedback (i.e.: no
  jog) when this step is performed correctly or incorrectly."*
- Gaposa's own cloud API **does not expose motor position or battery level**.
- The community linkIT integration sets `DEFAULT_TRAVEL_TIME = 60` s and describes its state model as
  *"Optimistic State"*. The whole ecosystem estimates position from elapsed time.

**Consequence:** three reliable **absolute** states — fully open, fully closed, and the motor's stored
intermediate position (all three are motor-side limits, so they re-datum on every full run). Everything
between is dead reckoning and drifts. **Store commanded position and estimated position as distinct values
with an explicit confidence/staleness flag — never record an estimate as though it were a measurement.**
Re-datum to a limit periodically.

---

## 4. Gaposa XS40-AC6024 motor

| Spec | Value |
|---|---|
| Supply | 120 VAC, 1.1 A nominal |
| Speed | **24 RPM, not adjustable** |
| Torque | 6.0 Nm (≈ 4.4 ft·lb) |
| Lift (2″ tube) | 53.1 lb |
| RF | 434.15 MHz |
| Programmable stops | **3** — open / closed / intermediate |
| Soft stop | yes, speed reduces approaching a stop |
| Noise | < 40 dBA |
| Body | 25″ × 1.47″ dia., ~3.6 lb |
| Cord | **8 ft non-removable, open-end, no plug** |

- **Full travel time: not documented.** Derivable estimate: 24 RPM = 0.4 rev/s; a bare 2″ tube is
  ≈ 159.6 mm circumference → ≈ 64 mm/s ≈ 2.5 in/s, rising as fabric builds on the roll. A 6 ft drop is
  therefore **~25–30 s** at the bare-tube rate. **Measure per shade.** The soft-stop ramp adds a non-linear
  tail at each end — calibrate between limits, not from a stopwatch on a partial run. Expect asymmetry
  (gravity assists DOWN).
- Limits and the intermediate position are stored **in the motor** and survive power loss.
- **Intermediate-position recall from a dry contact:** on a handheld remote this is *press and hold STOP
  for ≥ 3 s*. A held `St`→`com` closure of ~3 s should reproduce it, since the panel supports maintained
  closures up to 30 s. **LIKELY — inference, not documented. Verify.** Worth verifying: it is a third
  reliable absolute datum for free.

### Bench-testing the XS40

Gaposa's programming guide says the XS40/XS50 AC motors *"must be installed in tubes and mounted."* That
warning is aimed at running the motor as if installed — **a brief verification jog is a different thing and
is how pairing works anyway** (pairing makes the motor move). Precautions for a bench path-verification:

- **Clamp the motor body.** 6 Nm will twist out of a hand grip and take the cord with it.
- **Keep jogs to a second or two.** The thermal concern only applies to sustained running.
- **Make up the open-end 120 VAC cord before energizing**, dressed so leads cannot touch.
- **Do not set direction or limits on the bench** — both are position-dependent and stored in the motor.
  Order is: bench-verify path → mount in tube → set direction → set limits. (Direction reversal must
  precede limit setting.)
- Before limits are set, Gaposa motors run in **dead-man mode** — they move only while a command is held.
  That is the factory state and matches a click-and-hold verification naturally.

---

## 5. Alternative considered: Gaposa linkIT-US16 / US24

Gaposa lists this on the **same catalogue page** as the dry-contact panels, as the other integration route.
**Materially better suited to this project**, and Gaposa publishes the complete byte-level protocol.

| Spec | Value |
|---|---|
| Models | `linkIT-434-16` (16 ch) / `linkIT-434-24` (24 ch) |
| Power | **5 V DC, 0.3 A max**, micro-USB |
| RF | 434.15 MHz, 30 m / 98 ft, external antenna |
| Serial | **RS232 on RJ9**; RJ9→DB9 adapter included |
| RJ9 pinout | 1 = 5 V (optional power in), 2 = TXD, 3 = RXD, 4 = GND. DB9: 2→2, 3→3, 4→5 |
| LEDs | green on power; **red while transmitting**; blue when optional cloud connected |
| WiFi | 2.4 GHz, **optional cloud only** — RS232 operation is fully local |
| Price | ~$252–294 (vs ~$228–266 for the QCTZ36SDU) |

**Protocol — CONFIRMED** from Gaposa's own `linkit-rs232_en_2021.pdf`:

- **9600 8N1.**
- Host→hub: **5 bytes** — `0x67 | bank | channel | command | XOR(B0..B3)`
- `channel` is always **1–8 within a bank**. Banks: `0x00` = addr 1–8, `0x01` = 9–16, `0x02` = 17–24.

| Command | Byte |
|---|---|
| Add Motor (PROG TX) | `0xAA` |
| Delete Motor | `0xAB` |
| Go to Interim Position | `0xAD` |
| Tilt Up | `0xBA` |
| Tilt Down | `0xBB` |
| Stop | `0xCC` |
| Up | `0xDD` |
| Down | `0xEE` |

Worked examples from the PDF: bank 0 ch 1 DOWN → `0x67,0x00,0x01,0xEE,0x88`; bank 1 ch 1 UP →
`0x67,0x01,0x01,0xDD,0xBA`. Hub→host reply: **3 bytes** `0x66 | command | 0xFF` — a *command-received*
ACK, **not** shade state.

**Independently verified** by `max1234-cyber/gaposa_linkit` (Home Assistant custom component), whose
`_build_payload()` implements exactly `b4 = b0^b1^b2^b3` and whose bank mapping matches the PDF byte for
byte.

| | QCTZ36SDU | linkIT-US24 |
|---|---|---|
| Node interface | **18 isolated outputs** | **1 UART** |
| Channels | 6 | 24 |
| Commands | UP/STOP/DOWN | + Tilt Up/Down, Go-to-Interim |
| Pairing | physical buttons only | **`0xAA`/`0xAB` over the wire** |
| ACK | none | 3-byte ACK |
| Power | **120 VAC hard-wired, inside our enclosure** | **5 V USB** |
| Cross-channel conflict lockout | yes, box-wide | not documented as a constraint |

**Why it is better for us:** pairing over the wire removes the lost-remote operator dead-end; 5 V USB
power dissolves the mains-isolation unknown of §3.5 rather than requiring us to measure around it; one
UART replaces 18 relay channels.

**What it does not fix:** still no position feedback, still one-way 434.15 MHz to the motors, and `0xAA`
replaces the button press but not the procedure (an already-paired transmitter must still open the motor's
memory).

⚠️ **If adopted:** the document says RS232 and ships a DB9 adapter, so assume **true ±RS232 levels and use
a MAX3232-class level shifter — do not connect an ESP32 GPIO directly.** Gaposa's own warning: *"check for
crossover of pins 2 & 3 depending on the equipment used."* **Scope TXD before connecting anything.** The
RJ9 pin-1 5 V is marked *"advanced installation only and should not be used alongside the 5V micro USB
input"* — pick one.

**Status: open decision.** The QCTZ36SDU is already purchased and its dry-contact path genuinely works.

---

## 6. Module decomposition

Cut by **capability**, not by box, so the one-node-vs-two question stays a build-manifest line rather than
a refactor. See [ADR-0041](../adr/ADR-0041-hvac-shade-actuator-integration.md) for the rationale.

### Shared capability modules

| Module | Contract |
|---|---|
| `ha_dout` | Dry-contact / relay output. Fail-safe state asserted **before** the pin is configured as an output; non-strapping-GPIO requirement stated in the header; pulse primitive with a **guaranteed maximum width** enforced by an independent watchdog; minimum on/off dwell (short-cycle protection); commanded-vs-actual tracking. |
| `ha_rs485` | UART + half-duplex RS-485 transport. **Optional** DE/RE GPIO in `ha_rs485_cfg_t` (`-1` = auto-direction module), leaving the door open to ESP-IDF's native `UART_MODE_RS485_HALF_DUPLEX`. |

⚠️ **Naming:** the existing `ha_relay` component is the **BLE advert relay-coverage filter** (ADR-0015),
unrelated. Do not collide with it.

### Device-profile modules — thin; register/command maps + semantics, no transport code

| Module | Sits on | Carries |
|---|---|---|
| `ha_broan` | `ha_rs485` | Broan framing, checksum, token state machine, heartbeat, register map, E/W decode |
| `ha_aprilaire_dehum` | `ha_dout` | External-mode call semantics, cycle protection, commanded-vs-metered reconciliation |
| `ha_gaposa` | `ha_dout` | Pulse semantics, per-channel + **box-wide** interlocks, travel-time position model |

### Node builds

**Decided 2026-09-20:** `ha-shades` = a dedicated **ESP32-S3 N16R8** inside the QCT enclosure (chosen for
pin count — 18 channels direct, no expander); `ha-hvac` = a shared **ESP32-C6** carrying both
`ha_broan` and `ha_aprilaire_dehum`.

⚠️ **S3 N16R8 octal PSRAM consumes GPIO 33–37**; GPIO 26–32 are the SPI flash. Safe outputs: GPIO 1, 2,
4–18, 21, 39–42, 47, 48. Strapping pins 0/3/45/46 must never drive a shade contact.
⚠️ **C6 strapping pins are 4, 5, 8, 9, 15**; GPIO 24–30 flash, 12/13 USB, 16/17 UART0. `ha-hvac` is a
**XIAO ESP32-C6**, which breaks out D0–D10 — none of them strapping. Pinout: RS-485 TX/RX = **D10/D9
(GPIO18/GPIO20)** on **`UART_NUM_1`**, Aprilaire relay D1 (GPIO1), Broan OVR relay D2 (GPIO2). No DE pin
— see the transceiver table below.
⚠️ Its antenna switch is **software-controlled**, defaulting to the internal ceramic antenna; an external
U.FL antenna needs GPIO3 low + GPIO14 high in firmware.

⛔ **Do not put RS-485 on D6/D7 (GPIO16/17), and do not give `ha_rs485` `UART_NUM_0`.** *(Corrected
2026-09-22 — this doc previously specified D6/D7.)* GPIO16/17 are UART0, and `edge/esp32c6/sdkconfig`
sets `CONFIG_ESP_CONSOLE_UART_DEFAULT=y` with `CONFIG_ESP_CONSOLE_UART_NUM=0`, USB-Serial-JTAG only as the
**secondary** console. A transceiver on those pins means every reset dumps the ROM-bootloader banner and
the whole IDF boot log at 115200 baud onto the ERV's live bus, in parallel with a working wall control.
The `LISTEN_ONLY` gates in `ha_rs485` and `ha_broan` are application-layer and **cannot** stop the ROM
bootloader — the one guarantee bring-up depends on is exactly the one they can't make there. Picking
`UART_NUM_0` re-creates the same hazard on any pins, because `uart_set_pin()` drags the console along with
it. D10/D9 on UART1 are clear of strapping, flash and USB, and leave the console on its own pins.
⚠️ If `ha-hvac` gets its own build dir rather than reusing `edge/esp32c6`, **re-check the console config
there** — the hazard travels with any sdkconfig copied from that tree.

#### RS-485 transceiver — Waveshare **TTL TO RS485 (C)**, isolated

On hand 2026-09-22. Already named as auto-direction in `ha_rs485.h:24-26`.

| Board pad | Goes to | Note |
|---|---|---|
| `VCC` | XIAO **3V3** | silk offers 3.3 V/5 V — **3V3 only**, C6 GPIOs are not 5 V tolerant |
| `GND` | XIAO `GND` | TTL side only |
| `RXD` | **D10 (GPIO18)** — C6 TX | crossover; see the swap test below |
| `TXD` | **D9 (GPIO20)** — C6 RX | |
| `A+` | Broan J9 **`D-`** | ⚠️ `D+`→`B-`, `D-`→`A+`. Counterintuitive but correct; backwards is non-destructive and just logs `Alignment: Unexpected XX` |
| `B-` | Broan J9 **`D+`** | |
| `PE` | Broan J9 `GND` | Waveshare wiki calls `PE` *"RS485 Signal Ground"* — the **isolated**-side reference, not a chassis/shield terminal |

- ⛔ **Galvanically isolated — do not bond XIAO `GND` to Broan `GND`.** Digital isolator plus an onboard
  isolated DC-DC. TTL-side `GND` serves the XIAO, `PE` serves the ERV, and the two never meet. (Earlier
  bring-up notes said "bond grounds"; that was written for a *non-isolated* adapter, where it is required.
  With this part it throws the isolation away for nothing.)
- **Auto-direction, `de_gpio = -1`.** The TTL side is only `GND/RXD/TXD/VCC` — no DE/RE — so the module
  keys the driver off the `TXD` line itself (`ha_rs485.c:45` auto-direction path). **D3/GPIO21 is
  therefore free**; the DE reservation this doc used to carry does not apply to this board.
- **Fit a 10 kΩ pull-up from the C6 TX line to 3V3.** GPIO18 floats as an input from reset until
  `uart_set_pin()` runs, and on an auto-direction module a low `TXD` turns the driver *on* — that would
  put us on a live bus during precisely the window where firmware has no say. The pull-up parks the line
  at mark (driver off) from power-on. Alternatively, for the `LISTEN_ONLY` phase, simply don't land the TX
  wire at all: only the board's data-out is needed to sniff. (`ha_rs485` still rejects `tx_gpio < 0` at
  `ha_rs485.c:37`, so configure D10 either way and leave the wire off the header.)
- **Onboard 120 Ω is "enabled via soldering"** — open from the factory. ✅ **Verified 2026-09-22:** `A+`↔`B-`
  measures **10 kΩ** on our board, so the jumper is open; that reading is the fail-safe bias network and
  the transceiver's own ≥12 kΩ input load. ~120 Ω would have meant a bridged jumper and a third terminator
  on an already-terminated working bus — see the bench checklist.
- **`RXD`/`TXD` orientation is not stated unambiguously** by the wiki (`TXD` = *"TTL Signal Transmitting
  Pin"*, which reads either way). Crossover — MCU TX → board `RXD` — is the standard convention and the
  likelier reading, so try it first. For a listen-only sniff only **one** wire actually matters, the
  board's data-out into the C6 RX; if the log stays silent, move that single wire to the other pad.
  Non-destructive, two positions.

**`ha_modbus` is explicitly not being built.** Nothing in this set speaks Modbus; building it would be
speculative.

---

## 7. Bench checklists

### 7.1 QCT — no motor required

Mains live with the lid off: clip meter leads before energizing rather than probing live; nothing
conductive resting on the enclosure.

**Powered, terminals unconnected:**

1. **`com` → protective earth, AC *and* DC volts.** ⛔ **This is the gate.** Near zero = isolated input
   section, node-inside-enclosure is viable. Anything meaningfully above zero = mains-referenced, the node
   does **not** go in that box, and the plan changes. **Report this before proceeding.**
2. **DC volts `Up`→`com`, `St`→`com`, `Dw`→`com`**, both meter polarities. Gives open-circuit voltage and
   whether `com` sources or sinks.
3. **Close each input through 1 kΩ**, measure current → sink/source current; tells us whether a relay
   contact will wet properly.

**Powered off:**

4. **Continuity between `com` on CH1 and CH2–CH6.** Bonded = one common rail for all 18 outputs. Independent
   = every channel needs its own isolated pair.

**Behavioural, watching the LEDs:**

5. **Hold a closure 35 s** → confirm all LEDs light. **Then determine how it clears** — release, or power
   cycle? Highest-value behavioural test.
6. **Close CH1 `Up` + CH2 `Dw` simultaneously** → confirm the box-wide lockout and how it clears.
7. Check whether a channel LED flashes on transmit — if so, minimum pulse width can be swept without a motor.

### 7.2 Broan ERV

1. **Meter J9 `12V`→`GND` before landing anything.** Confirm 12 VDC and that you are not on J13.
2. ~~**Meter the transceiver `A+`↔`B-`, unpowered and off the bus.**~~ ✅ **DONE 2026-09-22 — 10 kΩ**, so
   the onboard 120 Ω jumper is open. Re-run this on any *replacement* board: ~120 Ω means the jumper is
   bridged, and hanging a third terminator on an already-terminated working bus is how you turn it
   marginal.
3. **Confirm XIAO `GND` and Broan `GND` are *not* bonded.** The converter is isolated; a stray bond
   defeats it silently and everything still appears to work.
4. **LISTEN_ONLY build with the wall control still attached.** Validates wiring, polarity, baud, checksum
   at zero risk.
5. Read-only active; confirm `02 60` → model string.
6. **Determine E50 recovery behaviour** — drop the bus > 5 s deliberately and observe whether the unit
   recovers on reconnect or needs a power cycle. ⛔ Blocking for the OTA story.
7. Confirm temperature units (°C or °F) against a known reference — the component publishes the raw float
   with no conversion and no document states which.
8. Confirm whether the recirculation damper (J6) is fitted before trusting mode `0x06`.

### 7.3 Aprilaire E070

1. **Measure `DH`–`DH` powered, relay disconnected** — AC *and* DC ranges, and to chassis ground. Anything
   above ~30 V: stop and reassess. Measure short-circuit current too.
2. **Confirm the `EXTERNAL` installer screen exists** on this SKU. If absent, `DH` may be inert here and
   RS-485 becomes the only path.
3. **Locate the `NC|NO` slide switch.** If absent, determine polarity empirically by jumpering `DH`–`DH`.
4. **Verify the relay module is electromechanical** (audible click, physical can), not an SSR.
5. Measure the module's actual coil current at 3.3 V; confirm the flyback diode is present.
6. **Characterize External-mode timing** — log commanded state vs metered power to learn the real
   anti-short-cycle and defrost intervals *before* writing control logic, or our controller will fight the
   unit's internal protections.

---

## 8. Open questions ledger

| # | Question | Blocks | Owner |
|---|---|---|---|
| 1 | ~~QCT `com`→earth: mains-referenced?~~ **RESOLVED 2026-09-20** — every input is opto-isolated (18× PC817A). Node-inside-enclosure is fine. | — | done |
| 2 | How does the QCT "all LEDs ON" error clear? | `ha_gaposa` recovery path | bench |
| 3 | QCT minimum recognized pulse width | `ha_gaposa` pulse constant | bench |
| 4 | Does a ~3 s `St` hold recall the intermediate position? | Third position datum | bench |
| 5 | **Does Broan E50 self-clear, or need a power cycle?** | **OTA story for `ha-hvac`** | bench |
| 6 | Broan temperature units (°C/°F) | `ha_broan` scaling | bench |
| 7 | Broan defrost register | Defrost telemetry | unresolved — needs a forced defrost + register diff |
| 8 | Broan AUTO mode enum value | Mode completeness | unresolved |
| 9 | Aprilaire `DH` voltage/current | Relay sizing; safety | bench |
| 10 | Does the plain E070 have an `EXTERNAL` screen? | Whether the relay path works at all | bench |
| 11 | Aprilaire `NC\|NO` switch present on E070? | Fail-safe direction | bench |
| 12 | Per-shade full travel time (both directions) | Position model | post-install |
| 13 | **linkIT vs QCTZ36SDU** | `ha_gaposa` transport | **Hugh — open decision** |
| 15 | ~~`Up`→`com` drive voltage and polarity~~ **RESOLVED 2026-09-21: 16.55 V, `com` = rail negative.** Direct GPIO drive is out; NPN low-side switch per channel. | — | done |
| 16 | ~~Are the six `com` terminals bonded?~~ **RESOLVED 2026-09-21: yes, all shorted.** One ground wire. | — | done |
| 17 | ~~Transformer VA rating~~ **MOOT 2026-09-21** — a separate 5 V wall-wart supply is wire-nutted in parallel off F/N, so the S3 does not load the QCT transformer at all. Its floating negative ties to `com`, which is also S3 GND. | — | done |
| 18 | Series resistor value (macro photo or in-circuit read) | exact LED current; confirms rail sag margin | bench, low priority |
| 14 | Broan `08 E0`/`09 E0` — real humidity or artifact? | Whether we can read RH from the ERV | bench, low priority |

---

## 9. Sources

**Broan**
- [Official AI Series Installation Guide & User Manual (52 pp.)](https://www.broan-nutone.com/getmedia/70f1313f-9bd0-4780-90b4-fe5189533f4f/Installation_Guide_Users_Manual_AI_Series.pdf) — J9 pinout, 24 VAC warning, OVR BAL/PER/DIS, J13 dry contact + auto schedule, full E/W tables, LCD mode table, service parts, wiring diagram
- [`nspitko/broan_erv_uart`](https://github.com/nspitko/broan_erv_uart) — the register map (`broan.h`), framing/checksum/token (`broan.cpp`), write sequences (`broan_control.cpp`), 8N1 enforcement (`__init__.py`)
- [`broan_erv_uart` issue #15](https://github.com/nspitko/broan_erv_uart/issues/15) — Tier B registers bench-validated on AM1G4
- [spitko.net — Reverse Engineering an ERV](https://spitko.net/2025/08/08/Reverse-Engineering-an-ERV/) — protocol derivation
- [HA community thread #339770](https://community.home-assistant.io/t/venmar-vanee-broan-erv-hvac-controller-output/339770) — years of failed Modbus attempts (negative result); Shelly-on-OVR precedent

**Aprilaire**
- [E070 Installation & Owner's Manual (10020790B)](https://crawlspacedepot.com/content/aprilaire-e070-installation-guide.pdf) — specs, Fig 15/16 terminal block, float-switch jumper, installer setup, `E1`–`E9`
- [E080/E100/E100H Installation Guide](https://abrwholesalers.com/media/assets/product/documents/aprilaire/dehumidifier/eseries/aprilaire-e080-e100-e100h-dehumidifier-installation-guide.pdf) — **the key document**: the "not a triac or other semiconductor" wording, the `NC|NO` switch, the `EXTERNAL` screen, HVAC block wiring
- [E070W/E080W/E100W/E130W Wi-Fi Manual (10021212)](https://assets-f02205d260.cdn.insitecloud.net/b766adfdea2dad8/aprilaire-e070w-e080w-e100w-e130w-wi-fi-dehumidifier-installation-owners-manual-10021212.pdf) — External-via-DH, Internal/Remote/External control-source table, air-sampling bypass
- [Model 76 Dehumidifier Control Instructions](https://manuals.totalhomesupply.com/wp-content/uploads/manuals/Model_76_Installation_Instructions-2.pdf) — "Dry Contact, Normally Open"; Remote = 9 VDC; the older 1710A `DH`→`Rf` arrangement
- [`dwrice0/aprilaire_controller`](https://github.com/dwrice0/aprilaire_controller) — ESP32-C6 + MAX485 on the E070 `A`/`B`; M-/R-frame format

**Gaposa**
- [QCTZ3SDU/QCTZ36SDU instructions (`QCT3SD_QCT36SD_I_ML_0614`)](https://www.avoutlet.com/images/product/additional/g/gaposa-qctz3_36sdu-ins.pdf) — **primary source**: terminal layout, momentary-closure commands, 30 s limit, mixed-signal error rule + both worked examples, pairing, 120 V/434.15 MHz/315 mA
- [Gaposa USA catalogue](https://www.gaposa.it/res/ftpgaposa/resources/catalogues/Catalog-USA-Exterior.pdf) p. 13 — QCTZ3SDU = 1 ch / QCTZ36SDU = 6 ch; linkIT listing
- [linkIT RS232 protocol](https://www.gaposa.it/media/1111/linkit-rs232_en_2021.pdf) — complete 5-byte frame spec, bank map, command bytes, RJ9/DB9 pinout
- [`max1234-cyber/gaposa_linkit`](https://github.com/max1234-cyber/gaposa_linkit) — independent confirmation of the linkIT frame
- [XS30/40/50 programming guide](https://www.avoutlet.com/images/product/additional/g/gaposa-xs30-40-50-rf-prog.pdf) — no motor-head program button; mount-before-running; remotes are dummy devices; pair/limit/intermediate/reset procedures
- [Emitto Smart Line manual](https://www.gaposa.it/media/1128/3istr36-z-qr-en.pdf) — `QCTZ` generation, 434.15 MHz, intermediate position = hold STOP ≥ 3 s

**Negative results worth recording**
- `merbanan/rtl_433` — **no Gaposa decoder exists.** Verified against a fresh shallow clone; 331 files in
  `src/devices`, `grep -ri gaposa` returns nothing. Only `somfy_rts.c` and `somfy_iohc.c` are shade-related.
- Broan **Overture** — cloud-only, no local API, no HA integration.
- `pyaprilaire` / official HA Aprilaire integration — thermostats and zone controls only; **not** the E070.
- Broan ERV **0–10 V analog control** — does not exist on this platform.
