// Panel data replica — ADR-0022 Phase 2 (incremental `since` sync). See rollup-ladder-and-replica-sync.md.
//
// Mirrors the server's compact rung sqlite to the SD card as `/sdcard/rungs.db`. Cold start seeds
// once from `/api/v1/rung/full.db`; thereafter each cycle reads `/api/v1/rung/manifest.json`
// (per-rung latest_bucket_start) and, for any rung behind its local high-water-mark, pulls only the
// delta from `/api/v1/rung/since?res&after=<hwm>` (NDJSON) and upserts by PK — no more 38 MB re-pull
// (Phase 1a), so freshening is cheap. Presence-gated on the SD card; charts query it locally
// (offline/instant, Phase 1b). A leaf module — deps: esp_http_client (linked for OTA) + ha_sdcard.
#pragma once

// Start the replica sync task against BFF `base` (http://host:port). Idempotent per boot; the task
// waits for the SD mount itself, so ordering vs ha_sdcard_mount() does not matter.
void ha_replica_start(const char *base);

// Force the #7 instance-backup files lane to run NOW (cmd/replica), instead of waiting for its ~hourly
// cycle. Spawns a one-shot task (blocking HTTP off the caller's stack); no-op if a sync is already running.
void ha_replica_sync_files_now(void);

// Query the local rung replica (Phase 1b): the resolution-selected `vmean` series for
// (device_id, metric) over the last `hours`, written oldest→newest into out[cap]. Returns the row
// count (0 if the device/metric has no rung rows in any resolution, or -1 if no replica is present
// yet — in which case the caller should fall back to the network). Picks the starting rung by span
// like the server's select_resolution (panel has no raw, so ≤2d→1min, ≤2mo→1hour, ≤4y→1day, else
// 1week), then ESCALATES coarser on an empty rung (1min→1hour→1day→1week) — the server keeps 1min
// only ~7d, so an older window resolving to 1min would otherwise find nothing. Serialized against the
// replica writer by an internal mutex; opens sqlite on the CALLER's task, so give it a generous
// stack (≥16 KB).
int ha_replica_rung_query(const char *device_id, const char *metric, int hours, double *out, int cap);

// SD hot-plug hooks (driven by ha_sdcard_watch's on_change). Insert: re-inventory the local
// replica so chart queries can use it immediately instead of waiting for the next sync cycle.
// Remove: mark the local cache gone so ha_replica_rung_query() falls back to the network rather
// than touching the dead mount.
void ha_replica_sd_inserted(void);
void ha_replica_sd_removed(void);
