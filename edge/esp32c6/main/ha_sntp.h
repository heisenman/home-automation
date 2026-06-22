#pragma once
#include <stdbool.h>
// Start SNTP against `server` and block until the clock is set (or timeout_ms).
bool ha_sntp_sync(const char *server, int timeout_ms);
// Spawn a background task that re-syncs SNTP every interval_ms (clamped >= 60s). The C6 RTC drifts, so
// a boot-only sync eventually fails the command freshness check (a clock-lockout that blocks OTA).
void ha_sntp_start_periodic(int interval_ms);
// ISO-8601 UTC "YYYY-MM-DDTHH:MM:SSZ" into buf (>=21 bytes). Returns false if time not set.
bool ha_sntp_iso_utc(char *buf, int buf_len);
