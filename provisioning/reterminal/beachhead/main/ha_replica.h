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

// Query the local rung replica (Phase 1b): the resolution-selected `vmean` series for
// (device_id, metric) over the last `hours`, written oldest→newest into out[cap]. Returns the row
// count (0 if the device/metric has no rung rows), or -1 if no replica is present yet — in which
// case the caller should fall back to the network. Picks the rung by span like the server's
// select_resolution (panel has no raw, so ≤2d→1min, ≤2mo→1hour, else 1day). Serialized against the
// replica's file swap by an internal mutex; opens sqlite on the CALLER's task, so give that task a
// generous stack (≥16 KB).
int ha_replica_rung_query(const char *device_id, const char *metric, int hours, double *out, int cap);
