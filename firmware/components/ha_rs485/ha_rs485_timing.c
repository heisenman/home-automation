// ha_rs485_timing — pure serial timing arithmetic (ADR-0041). No IDF deps, host-tested.
// See include/ha_rs485_timing.h for the contract.
#include "ha_rs485_timing.h"

// Round-up integer division. Every result here sizes a timeout or a guard delay, so rounding toward zero
// would make them marginally too short — the failure mode is intermittent and miserable to diagnose.
static inline uint32_t div_up(uint64_t num, uint32_t den) {
    if (den == 0) return 0;
    return (uint32_t)((num + den - 1u) / den);
}

uint32_t ha_rs485_bits_per_char(uint8_t data_bits, bool parity, uint8_t stop_bits_x2) {
    if (data_bits < 5u) data_bits = 5u;
    if (data_bits > 8u) data_bits = 8u;
    if (stop_bits_x2 < 2u) stop_bits_x2 = 2u;   // 1.0 stop bit
    if (stop_bits_x2 > 4u) stop_bits_x2 = 4u;   // 2.0 stop bits

    // Count in HALF bits so 1.5 stop bits stays exact, then round the total up to whole bit times —
    // the line still occupies a full bit period for a half stop bit.
    uint32_t half = 2u                       // start bit
                  + (uint32_t)data_bits * 2u
                  + (parity ? 2u : 0u)
                  + (uint32_t)stop_bits_x2;
    return div_up(half, 2u);
}

uint32_t ha_rs485_char_us(uint32_t baud, uint32_t bits_per_char) {
    if (baud == 0) return 0;
    return div_up((uint64_t)bits_per_char * 1000000u, baud);
}

uint32_t ha_rs485_tx_us(uint32_t baud, uint32_t bits_per_char, uint32_t nbytes) {
    if (baud == 0 || nbytes == 0) return 0;
    // Compute over the whole burst rather than multiplying a rounded per-character figure, so the
    // rounding error stays at one microsecond instead of accumulating per byte.
    return div_up((uint64_t)bits_per_char * nbytes * 1000000u, baud);
}

uint32_t ha_rs485_turnaround_us(uint32_t baud, uint32_t bits_per_char, bool auto_direction) {
    if (!auto_direction) return 0;   // a DE pin is driven in hardware with exact timing
    return ha_rs485_char_us(baud, bits_per_char) * 2u;
}

uint32_t ha_rs485_silent_us(uint32_t baud, uint32_t bits_per_char) {
    if (baud == 0) return 0;
    if (baud > 19200u) return 1750u;   // Modbus RTU caps t3.5 here rather than letting it shrink
    // 3.5 character times, kept in integer arithmetic: (7 * bits * 1e6) / (2 * baud).
    return div_up((uint64_t)bits_per_char * 7u * 1000000u, baud * 2u);
}
