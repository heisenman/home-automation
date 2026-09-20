// BREADCRUMB: firmware/components > ha_rs485 - pure serial timing arithmetic (char time, TX duration, turnaround, Modbus-style silent interval). No IDF deps, host-tested. Contract: ADR-0041. Parent: firmware/AGENTS.md.
// REUSE-WHEN: sizing a half-duplex RS-485 timeout, a direction-turnaround delay, or an inter-frame gap.
#pragma once
#include <stdbool.h>
#include <stdint.h>

// Split out from the IDF transport deliberately: this is the part of RS-485 that is pure arithmetic, so
// it can carry a host test (firmware/AGENTS.md: "pure modules ship a host test"). The transport itself is
// thin UART glue that only means anything against real hardware.

// Total bit times per character on the wire, including the start bit.
//   start(1) + data(5..8) + parity(0..1) + stop(1..2)
// stop_bits_x2 is in HALF stop bits, so 1.5 stop bits is representable: 2 => 1.0, 3 => 1.5, 4 => 2.0.
uint32_t ha_rs485_bits_per_char(uint8_t data_bits, bool parity, uint8_t stop_bits_x2);

// Microseconds for one character at this baud. Rounded UP — every consumer of this is sizing a timeout or
// a guard delay, and rounding down would make those marginally too short.
uint32_t ha_rs485_char_us(uint32_t baud, uint32_t bits_per_char);

// Microseconds to clock out `nbytes`. Used to size write timeouts and to know when the line is free.
uint32_t ha_rs485_tx_us(uint32_t baud, uint32_t bits_per_char, uint32_t nbytes);

// How long to hold off before trusting RX after transmitting.
//
// An AUTO-DIRECTION transceiver (no DE/RE pin — the Waveshare TTL-to-RS485 (C) and the HiLetgo boards are
// both this kind) derives direction from the TX line itself, typically with a one-shot that holds the
// driver enabled for a little after the last stop bit. Listening too early reads the tail of our own
// transmission. Two character times is the conventional margin.
//
// With an explicit DE pin the ESP32 UART drives it in hardware (UART_MODE_RS485_HALF_DUPLEX) with exact
// timing, so this is 0.
uint32_t ha_rs485_turnaround_us(uint32_t baud, uint32_t bits_per_char, bool auto_direction);

// Modbus-RTU inter-frame silence (t3.5): 3.5 character times, but FIXED at 1750 us above 19200 baud —
// the spec caps it there rather than letting it shrink indefinitely.
//
// ⚠ Broan does NOT specify an inter-frame gap; its frames are delimited by STX/ETX and the bus is
// token-passed, so ha_broan does not need this. It is here for the next device on this transport, and
// because a conservative silent interval is a reasonable default for any framing-by-silence protocol.
uint32_t ha_rs485_silent_us(uint32_t baud, uint32_t bits_per_char);
