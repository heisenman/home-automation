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

Two layers, **both pure** — no ESP deps, no UART, so the whole thing is provable on the host:

| File | What |
|---|---|
| `ha_broan_frame.c` | the wire codec — framing, checksum, register encode/decode, TLV walking |
| `ha_broan.c` | the session state machine — token handshake, ping/pong, heartbeat, chunked polling, field cache |

The state machine takes **bytes in and produces bytes out**. It never touches `ha_rs485`; the node glue
wires the two together. That is deliberate: it means the entire token conversation is driven against a
**simulated ERV** in the test suite rather than discovered on a live bus where a mistake shuts down a
ventilator.

### Session behaviour worth knowing

- **The heartbeat outranks user writes and polling** inside a token hold. E50 shuts the unit down; a
  backlog of queued writes must never be the reason we go quiet. Tested explicitly.
- **The first heartbeat goes out as soon as we hold the bus**, not `heartbeat_ms` later. We have just
  taken the wall control's slot and the control timeout is *shorter* than the heartbeat interval, so
  waiting a full interval to first assert liveness would be a gratuitous window on an E50. (The reference
  implementation waits; this does not.)
- **A write ack invalidates the cached register**, so the next sweep re-reads instead of reporting the
  pre-write value as current.
- **`ha_broan_e50_exposure_ms()`** reports how long since our last heartbeat landed, so a caller can alarm
  *before* the unit faults rather than discovering it afterwards.
- **A reply that never arrives does not wedge the hold** — it times out and the state machine makes
  progress, rather than idling until the next token offer.
- **Listen-only still harvests telemetry.** Register responses are cached regardless of which controller
  they were addressed to, so a listen-only build parked beside the real wall control validates the whole
  register map before we ever take the bus.

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

`run.sh` runs **two** suites — the codec and the state machine.

**Codec:** byte-exact vectors for the canonical heartbeat frame (`01 10 12 01 04 40 00 50 00 49 04`, checksum derived
by hand in the test comments), round-trip encode/decode, decoder robustness (partial frames, leading junk
resync, corrupt checksum, corrupt payload, bad alignment, bad footer), read/write encodings in all three
widths, TLV walking including truncated and stub runs, buffer-capacity guards, and the OVR refusal.

**State machine:** driven against a simulated ERV that pings, offers and reclaims the token, answers
reads from a register table, and acks writes. Covers ping/pong, the `0x04`/`0x05` handshake, heartbeat
cadence and its priority over queued writes, 13-register polling chunked into 10 + 3, token release when
idle, the field cache and value ages, write-ack invalidation, queue overflow accounting, E50 exposure,
the ERV refusing to yield the token, a swallowed reply, listen-only silence across every trigger,
cross-controller telemetry harvesting, split-frame reassembly, junk-then-frame resync, corrupt checksums,
and millisecond wraparound.

Register constants are additionally cross-checked against the design doc's wire-opcode table.
Compile-verified against ESP-IDF for esp32c6 with zero warnings.
