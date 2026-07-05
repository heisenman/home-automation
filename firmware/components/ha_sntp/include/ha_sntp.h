// BREADCRUMB: firmware/components > ha_sntp - best-effort clock sync: blocking initial SNTP + periodic re-sync (RC-oscillator RTC drifts) + ISO-8601 UTC helper, so the command freshness check never clock-locks the node. Contract: ADR-0015. Parent: firmware/AGENTS.md.
// REUSE-WHEN: any board needs a wall clock kept fresh enough to pass the signed-command freshness window (don't hand-roll SNTP init/re-sync).
#pragma once
#include <stdbool.h>
// Start SNTP against `server` and block until the clock is set (or timeout_ms).
bool ha_sntp_sync(const char *server, int timeout_ms);
// Spawn a background task that re-syncs SNTP every interval_ms (clamped >= 60s). The C6 RTC drifts, so
// a boot-only sync eventually fails the command freshness check (a clock-lockout that blocks OTA).
void ha_sntp_start_periodic(int interval_ms);
// ISO-8601 UTC "YYYY-MM-DDTHH:MM:SSZ" into buf (>=21 bytes). Returns false if time not set.
bool ha_sntp_iso_utc(char *buf, int buf_len);
