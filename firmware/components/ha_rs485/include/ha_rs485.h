// BREADCRUMB: firmware/components > ha_rs485 - half-duplex RS-485 UART transport: auto-direction OR hardware DE/RE, listen-only gate, echo suppression. Contract: ADR-0041. Parent: firmware/AGENTS.md.
// REUSE-WHEN: any node that has to speak a byte protocol over an RS-485 pair. Protocol-agnostic by design
// — ha_broan sits on top of this and nothing Broan-specific lives here.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "driver/uart.h"
#include "esp_err.h"
#include "ha_rs485_timing.h"

// ADR-0041 §1. Bytes in, bytes out, correct direction control. Framing is the caller's business.
//
// Largest single write. Broan's maximum frame is 262 bytes; the margin covers the echo-suppression
// staging buffer without making the struct large.
#define HA_RS485_MAX_TX 288

typedef struct {
    uart_port_t port;
    int         tx_gpio;
    int         rx_gpio;

    // ⚠ THE ADAPTER QUESTION. Set to -1 for an AUTO-DIRECTION transceiver — one whose TTL side has only
    // VCC/TXD/RXD/GND, which is what both the Waveshare TTL-to-RS485 (C) and the HiLetgo boards are. The
    // module then works out direction from the TX line itself.
    //
    // Give a real GPIO only if the transceiver exposes a DE/RE pin. That path is strictly better where it
    // exists: the ESP32 UART drives it in hardware via UART_MODE_RS485_HALF_DUPLEX with exact turnaround
    // timing, instead of an RC one-shot guessing. Both are supported so the module choice stays a wiring
    // decision rather than a code change.
    int         de_gpio;

    uint32_t         baud;          // 38400 for Broan
    uart_word_length_t data_bits;   // default UART_DATA_8_BITS
    uart_parity_t      parity;      // default UART_PARITY_DISABLE
    uart_stop_bits_t   stop_bits;   // default UART_STOP_BITS_1

    size_t rx_buf_bytes;            // driver RX ring; 0 => 2048

    // ⛔ THE SAFETY GATE. When true, every write is refused at the transport layer and counted. This is
    // bring-up step 1 from ADR-0041 §1.12: sniff the ERV's real conversation with its wall control still
    // attached, validating wiring, polarity, baud and checksum at ZERO bus writes.
    //
    // Enforcing it here rather than in the protocol layer means no amount of state-machine bugs can put a
    // byte on a live bus. Taking the Broan bus makes the ERV depend on us; earning that should require
    // deliberately clearing this flag.
    bool listen_only;

    // Some cheap auto-direction transceivers loop transmitted bytes back into RX. When set, a write
    // reads back and drops only the bytes that actually match what was sent — anything that diverges is
    // held for the next read rather than eaten. Leave false until a board is observed doing it; the
    // alternative defence is to filter received frames by sender address, which ha_broan does anyway.
    bool discard_echo;
} ha_rs485_cfg_t;

typedef struct {
    uint64_t rx_bytes;
    uint64_t tx_bytes;
    uint64_t echo_bytes;     // suppressed loopback
    uint32_t writes_refused; // blocked by listen_only — expected to be non-zero during bring-up
    uint32_t tx_timeouts;
} ha_rs485_stats_t;

typedef struct {
    ha_rs485_cfg_t cfg;
    bool     inited;
    bool     auto_direction;
    uint32_t bits_per_char;
    uint32_t turnaround_us;
    ha_rs485_stats_t stats;

    // Bytes read during an echo check that turned out not to be echo. Served before the UART on the next
    // read, because the IDF driver has no push-back and dropping a real reply would be a silent data loss.
    uint8_t  hold[HA_RS485_MAX_TX];
    size_t   hold_len;
    size_t   hold_pos;
} ha_rs485_t;

// Configures the UART, installs the driver, and selects the direction strategy. Safe to call once per
// port. Returns ESP_ERR_INVALID_ARG for a bad config.
esp_err_t ha_rs485_init(ha_rs485_t *b, const ha_rs485_cfg_t *cfg);
esp_err_t ha_rs485_deinit(ha_rs485_t *b);

// Writes and waits for the line to clear (so a caller knows when it may listen).
// Returns ESP_ERR_NOT_SUPPORTED — without transmitting — when listen_only is set.
// Returns ESP_ERR_INVALID_SIZE if len > HA_RS485_MAX_TX.
esp_err_t ha_rs485_write(ha_rs485_t *b, const uint8_t *data, size_t len, uint32_t timeout_ms);

// Returns bytes read (0 on timeout), or negative on error. Serves any held non-echo bytes first.
int ha_rs485_read(ha_rs485_t *b, uint8_t *out, size_t max, uint32_t timeout_ms);

esp_err_t ha_rs485_flush_input(ha_rs485_t *b);

void ha_rs485_get_stats(const ha_rs485_t *b, ha_rs485_stats_t *out);
bool ha_rs485_is_listen_only(const ha_rs485_t *b);

// Microseconds this transport needs to clock out `nbytes` — for sizing caller-side timeouts.
uint32_t ha_rs485_tx_time_us(const ha_rs485_t *b, uint32_t nbytes);
