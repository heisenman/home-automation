# ha_rs485 — half-duplex RS-485 transport

**Contract:** [`include/ha_rs485.h`](include/ha_rs485.h) · [`include/ha_rs485_timing.h`](include/ha_rs485_timing.h)
· **ADR:** [ADR-0041](../../../docs/adr/ADR-0041-hvac-shade-actuator-integration.md)
· **Design:** [hvac-shade-device-integration §1](../../../docs/design/hvac-shade-device-integration.md)

Bytes in, bytes out, correct direction control. Framing is the caller's business — `ha_broan` sits on top
and nothing Broan-specific lives here, so the next serial device is a consumer rather than a fork.

## Split in two on purpose

| File | What | Proven by |
|---|---|---|
| `ha_rs485_timing.c` | pure arithmetic — character time, TX duration, turnaround, Modbus t3.5 | **host test** (`test/run.sh`) |
| `ha_rs485.c` | thin IDF UART glue | compiles clean for c6 / s3 / c3; needs hardware for the rest |

Splitting the arithmetic out is what lets any of this be tested off-target, per the firmware
module rules. The transport half genuinely can't be — it's UART configuration, and it only means
something against a real transceiver.

## The adapter question

Set `de_gpio = -1` for an **auto-direction** transceiver — one whose TTL side has only VCC/TXD/RXD/GND.
Both the Waveshare TTL-to-RS485 (C) and the HiLetgo boards are this kind; the module works direction out
from the TX line itself.

Give a real GPIO only if the transceiver exposes **DE/RE**. That path is strictly better where it exists:
the ESP32 UART drives it in hardware via `UART_MODE_RS485_HALF_DUPLEX` with exact turnaround timing,
instead of an RC one-shot guessing. Both are supported so which module you buy stays a wiring decision
rather than a code change.

With auto-direction, `ha_rs485_write()` holds off for two character times after the line clears before
trusting RX — those transceivers keep the driver enabled briefly past the last stop bit, and listening
through that window reads the tail of your own transmission.

## ⛔ The listen-only gate

`cfg.listen_only` makes every write fail with `ESP_ERR_NOT_SUPPORTED`, without transmitting, and counts it.

This is bring-up step 1 from ADR-0041 §1.12: sniff the ERV's real conversation with its **wall control
still attached**, validating wiring, polarity, baud and checksum at *zero* bus writes.

It lives at the transport layer deliberately. Enforcing it here rather than in the protocol layer means no
amount of state-machine bugs can put a byte on a live bus. Taking the Broan bus makes the ERV depend on us
— it faults with E50 and shuts down if we go quiet — so earning that should require deliberately clearing
a flag, not merely not-reaching a code path.

## Echo suppression

Some cheap auto-direction transceivers loop transmitted bytes back into RX. `cfg.discard_echo` reads back
after a write and drops **only bytes that actually match what was sent** — anything that diverges is a real
reply and gets held for the next `ha_rs485_read()`, because the IDF driver has no push-back and silently
eating a reply would surface much later as an inexplicable protocol bug.

Leave it false until a board is observed doing it. The alternative defence is filtering received frames by
sender address, which `ha_broan` does anyway.

## Test

```sh
firmware/components/ha_rs485/test/run.sh    # plain cc, no IDF — timing core only
```

Covers bits-per-character across data/parity/stop combinations (including 1.5 stop bits and out-of-range
clamping), character time, transmission time, turnaround for both direction strategies, and the Modbus
t3.5 rule either side of its 19200-baud break.

Two properties worth their assertions: transmission time is computed over the **whole burst** rather than
as `nbytes ×` a rounded per-character figure, so rounding error stays at one microsecond instead of
compounding per byte (the test pins both numbers so the difference is visible); and the intermediate is
64-bit, since `bits × nbytes × 1e6` overflows 32 bits on any realistic frame.

## Verification status

Compile-verified for **esp32c6, esp32s3, esp32c3** with zero warnings. **Not hardware-verified** — the
adapter arrives Mon 2026-09-22 and the ERV after it. First real use should be a listen-only build.
