// aprilaire_sniff — LISTEN-ONLY RS-485 sniffer on the Aprilaire E070 Remote A/B terminals (ADR-0041).
//
// Question it answers: does the E070 put anything on A/B with no Model 76 remote attached, and if so what
// (prior art dwrice0/aprilaire_controller describes M-/R-frames at 9600 8N1)? No protocol is assumed:
// bytes are split into frames on line-idle gaps and logged raw (hex + printable ASCII) for analysis.
//
// ⛔ It can never transmit: ha_rs485 is opened with listen_only=true, which refuses every write at the
// transport layer, and this module never calls ha_rs485_write at all.
#pragma once
#include <stdbool.h>

// Open UART1 on D10/GPIO18 (TX, never driven by us) + D9/GPIO20 (RX) and start the reader task.
// `log` publishes a diagnostic line (ha_mqtt_log); called from the reader task.
bool aprilaire_sniff_start(void (*log)(const char *fmt, ...));

// Log the counters + a verdict + the most recent frames now (also runs every 30 s on its own).
void aprilaire_sniff_report(void);
