// Panel data replica — ADR-0022 Phase 1a (seed pull). See rollup-ladder-and-replica-sync.md.
//
// Mirrors the server's compact rung sqlite (`/api/v1/rung/full.db`) to the SD card as
// `/sdcard/rungs.db`, kept fresh by polling `/api/v1/rung/manifest.json` (sha256 compare — only
// re-pulls when the server DB actually changed). Presence-gated on the SD card. This is the
// plumbing tier: it lands + freshens the replica; charts querying it locally (offline/instant) is
// Phase 1b. A leaf module — deps: esp_http_client (already linked for OTA) + ha_sdcard.
#pragma once

// Start the replica sync task against BFF `base` (http://host:port). Idempotent per boot; the task
// waits for the SD mount itself, so ordering vs ha_sdcard_mount() does not matter.
void ha_replica_start(const char *base);
