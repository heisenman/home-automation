# ha_broan — Broan AI Series ERV wire protocol (RS-485)

**Contract:** [`include/ha_broan_frame.h`](include/ha_broan_frame.h) · **ADR:** [ADR-0041](../../../docs/adr/ADR-0041-hvac-shade-actuator-integration.md)
· **Design:** [hvac-shade-device-integration §1](../../../docs/design/hvac-shade-device-integration.md)

Talks to a Broan / NuTone / Venmar / vanEE / Best **AI Series (AM1 platform)** HRV or ERV over its
`D+`/`D-` wall-control bus. Verified against `nspitko/broan_erv_uart`, the reverse-engineered reference.

## ⛔ This is not Modbus

Broan's 52-page installer manual never mentions Modbus, RS-485, or baud rate, and Broan declined to publish
a spec. The Home Assistant community spent years cycling baud and parity against a CRC **that does not
exist** — the checksum is an 8-bit sum with deliberate overflow. `ha_modbus` is deliberately not built
(ADR-0041 §3); the shared seam is `ha_rs485`.

```
frame:  0x01 | target | sender | 0x01 | len | payload[len] | checksum | 0x04
```

38400 8N1, half-duplex, and **token-passed rather than master/slave polled**: the ERV offers the bus with
`0x04` and we may transmit only while holding it.

| Opcode | Meaning |
|---|---|
| `0x02` / `0x03` | ping (ASCII `"Ping"`) / pong |
| `0x04` / `0x05` | token offer / ack |
| `0x20` / `0x21` | register read request / response — **max 10 registers per request** |
| `0x40` / `0x41` | register write request / ack |

Opcodes are **big-endian on the wire**; payload values are **little-endian**. Both directions are covered
by tests, because getting this backwards is the easy mistake — and it was made once during development,
caught by the byte-exact vectors below.

## What is here

`ha_broan_frame.c` is the **pure codec** — framing, checksum, register encode/decode, TLV walking. No ESP
deps, no transport, so it is provable on the host. The transport-bound state machine (token handling,
heartbeat scheduling, poll cadence) layers on top of `ha_rs485` and is not written yet.

Values are extracted with explicit shifts rather than a union punt, so the codec is correct on a big-endian
host too — which is what makes the host test meaningful rather than accidentally passing.

## ⚠️ Two things that will bite

**`BROAN_FAN_OVR` (0x02) is never written.** It is a hard override that wedges the unit until cleared.
`broan_fan_mode_writable()` refuses it so a bad automation cannot reach it at all.

**Taking this bus makes the ERV less reliable, not more.** It tolerates exactly one controller and it is
the *wall control's* slot we take. Once we answer we owe it a heartbeat every 10 s; going quiet past 5 s
raises fault **E50 and the unit shuts down** — and that includes every OTA. Whether E50 self-clears or
needs a manual power cycle is **unresolved and blocking** (ADR-0041 §1.8). The `OVR` dry contact stays
wired in parallel as a failsafe that survives the node being dead.

## Register map

Tier A constants only — the ones read from the reference implementation's field table. The full map,
including the contested humidity registers and the do-not-write Tier C set, is in the design doc §1.4.

Note the ERV **has no humidity sensor**: `BROAN_REG_CTRL_HUMIDITY` is a value *we supply*. Broan's own
parts list and wiring diagram show only thermistors.

## Test

```sh
firmware/components/ha_broan/test/run.sh    # plain cc, no IDF
```

Byte-exact vectors for the canonical heartbeat frame (`01 10 12 01 04 40 00 50 00 49 04`, checksum derived
by hand in the test comments), round-trip encode/decode, decoder robustness (partial frames, leading junk
resync, corrupt checksum, corrupt payload, bad alignment, bad footer), read/write encodings in all three
widths, TLV walking including truncated and stub runs, buffer-capacity guards, and the OVR refusal.

Register constants are additionally cross-checked against the design doc's wire-opcode table.
