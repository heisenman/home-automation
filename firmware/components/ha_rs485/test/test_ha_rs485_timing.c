// Host unit test for the RS-485 timing arithmetic (no ESP deps). Run via ./run.sh, or manually:
//   cc test/test_ha_rs485_timing.c ha_rs485_timing.c -Iinclude -o /tmp/t && /tmp/t
//
// Only the pure half of ha_rs485 is testable here by design — the transport is UART glue that means
// nothing without hardware. What IS provable is the arithmetic every timeout and guard delay rests on.
#include "ha_rs485_timing.h"
#include <stdio.h>

static int fails = 0;
static void check(const char *name, int cond) {
    printf("%s  %s\n", cond ? "PASS" : "FAIL", name);
    if (!cond) fails++;
}

int main(void) {
    // ── bits per character ────────────────────────────────────────────────────
    check("8N1 = 10 bits",   ha_rs485_bits_per_char(8, false, 2) == 10);   // Broan
    check("8E1 = 11 bits",   ha_rs485_bits_per_char(8, true,  2) == 11);
    check("8N2 = 11 bits",   ha_rs485_bits_per_char(8, false, 4) == 11);
    check("7E1 = 10 bits",   ha_rs485_bits_per_char(7, true,  2) == 10);
    check("5N1 = 7 bits",    ha_rs485_bits_per_char(5, false, 2) == 7);
    // A half stop bit still occupies a whole bit period on the line, so the total rounds up.
    check("8N1.5 = 11 bits", ha_rs485_bits_per_char(8, false, 3) == 11);

    // Nonsense inputs clamp into range rather than producing a wild timeout.
    check("data<5 clamps",   ha_rs485_bits_per_char(4, false, 2) == 7);
    check("data>8 clamps",   ha_rs485_bits_per_char(9, false, 2) == 10);
    check("stop<1 clamps",   ha_rs485_bits_per_char(8, false, 0) == 10);
    check("stop>2 clamps",   ha_rs485_bits_per_char(8, false, 9) == 11);

    // ── character time ────────────────────────────────────────────────────────
    // 10 bits / 38400 = 260.4 us, rounded UP: every consumer is sizing a timeout.
    check("38400 8N1 -> 261 us", ha_rs485_char_us(38400, 10) == 261);
    check("9600 8N1 -> 1042 us", ha_rs485_char_us(9600, 10) == 1042);
    check("zero baud is 0",      ha_rs485_char_us(0, 10) == 0);

    // ── transmission time ─────────────────────────────────────────────────────
    // The Broan heartbeat frame is 11 bytes: 110 bits / 38400 = 2864.6 us.
    check("11-byte frame -> 2865 us", ha_rs485_tx_us(38400, 10, 11) == 2865);
    check("zero bytes is 0",          ha_rs485_tx_us(38400, 10, 0) == 0);
    check("zero baud is 0",           ha_rs485_tx_us(0, 10, 11) == 0);

    // Computed over the whole burst, NOT as nbytes * a rounded per-character figure — otherwise the
    // rounding error compounds per byte and a long frame's timeout drifts noticeably wide.
    check("no per-byte rounding drift", ha_rs485_tx_us(38400, 10, 100) == 26042);
    check("  (naive would give 26100)", ha_rs485_char_us(38400, 10) * 100 == 26100);

    // 64-bit intermediate: bits * nbytes * 1e6 overflows 32 bits well before this.
    check("large burst does not overflow", ha_rs485_tx_us(38400, 10, 100000) == 26041667);

    // ── turnaround ────────────────────────────────────────────────────────────
    // An auto-direction transceiver holds its driver enabled past the last stop bit; two character times
    // is the conventional margin before trusting RX.
    check("auto-direction = 2 chars", ha_rs485_turnaround_us(38400, 10, true) == 522);
    // A hardware DE pin is driven by the UART with exact timing, so no software guard is needed.
    check("hardware DE = 0",          ha_rs485_turnaround_us(38400, 10, false) == 0);

    // ── Modbus t3.5 silent interval ───────────────────────────────────────────
    // Fixed at 1750 us above 19200 baud; 3.5 character times at or below it.
    check("38400 -> fixed 1750",  ha_rs485_silent_us(38400, 10) == 1750);
    check("19201 -> fixed 1750",  ha_rs485_silent_us(19201, 10) == 1750);
    check("19200 -> 3.5 chars",   ha_rs485_silent_us(19200, 10) == 1823);
    check("9600 -> 3.5 chars",    ha_rs485_silent_us(9600, 10) == 3646);
    check("zero baud is 0",       ha_rs485_silent_us(0, 10) == 0);

    printf("\n%s (%d failure%s)\n", fails ? "FAILED" : "ALL PASS", fails, fails == 1 ? "" : "s");
    return fails ? 1 : 0;
}
