#pragma once
#include <stdbool.h>

// Over-the-air update via HTTP pull into the inactive OTA slot, with self-test + auto-rollback.
//
// Brick-safety (see PLAN-forwarder-ota.md): a freshly-OTA'd image boots in PENDING_VERIFY state.
// ha_ota_confirm_if_pending() runs a self-test (Wi-Fi + MQTT reachable) and only then marks the
// image valid; if the self-test fails — or the app hangs/crashes before reaching it — the
// bootloader automatically rolls back to the previous, known-good slot. Cable-flashed images are
// never in PENDING_VERIFY, so this is a no-op on bench builds.

// Call once at boot AFTER Wi-Fi/MQTT/BLE are started. Confirms or rolls back a trial image.
void ha_ota_confirm_if_pending(void);

// Start an OTA download from `url` (http or https) into the inactive slot. On success the node
// reboots into the new slot pending verification. On failure it stays on the current image.
// Returns false if an OTA is already running or the task can't start.
bool ha_ota_start(const char *url);

bool ha_ota_busy(void);
