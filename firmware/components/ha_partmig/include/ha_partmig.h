// BREADCRUMB: firmware/components > ha_partmig - one-time in-place partition-table migration off a stock-firmware layout (Tasmota safeboot) so an image uploaded through the vendor's own web OTA becomes a normal A/B node. Pure planner host-tested; IDF executor. Contract: ADR-0042. Parent: firmware/AGENTS.md.
// REUSE-WHEN: taking over an ESP32-family device that ships Tasmota (v12+ safeboot layout) — or any vendor
// firmware whose app slot already sits where our ota_0 can be placed — WITHOUT opening it for a serial flash.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// ── How it works (ADR-0042) ──────────────────────────────────────────────────────────────────────────────
// Tasmota 4 MB: nvs 0x9000 | otadata 0xE000 | safeboot (factory) 0x10000 | app0 (ota_0) 0xE0000 | fs.
// Tasmota's web upload writes OUR image into app0 and boots it. Our partition table puts ota_0 at that SAME
// offset, so nothing is copied: the node only rewrites the 3 KB table (keeping its own running slot), and the
// ex-safeboot and ex-fs space become our spare + ota_1. Order is chosen so that only the final table write is
// unrecoverable if power drops:
//   1. erase NVS (Tasmota's settings)              — power cut: Tasmota's table + our app, still ours, retried
//   2. erase otadata                               — power cut: old table boots Tasmota SAFEBOOT (re-upload)
//   3. erase + write + verify the table at 0x8000  — ~50 ms: the one window that would need a serial flash
//   4. restart → bootloader, blank otadata, no factory partition → boots ota_0 = this image.

#define HA_PARTMIG_TABLE_ADDR 0x8000u
#define HA_PARTMIG_TABLE_MAX  0xC00u     // ESP_PARTITION_TABLE_MAX_LEN

typedef enum {
    HA_PARTMIG_NONE = 0,       // table already ours (or byte-identical) — nothing to do
    HA_PARTMIG_MIGRATE,        // foreign safeboot layout, our slot lines up — rewrite it
    HA_PARTMIG_FOREIGN_UNSAFE, // foreign layout we can't take in place (slot mismatch, no safeboot...) — leave it
    HA_PARTMIG_BAD_INPUT,      // our embedded table is malformed — a build error
} ha_partmig_verdict_t;

typedef struct {
    uint32_t offset, size;
    uint8_t  type, subtype;
    char     label[17];
} ha_partmig_entry_t;

// Parse up to `max` entries of a raw table (stops at the first non-0x50AA entry; skips the MD5 entry).
int ha_partmig_parse(const uint8_t *raw, size_t len, ha_partmig_entry_t *out, int max);

// Decide. `cur` = the raw table on flash, `ours` = the table this image was built with, `running_addr` = the
// flash offset this image executes from, `image_len` = its size. MIGRATE only when ALL hold: `cur` has an app
// partition labelled "safeboot"; `cur` has an app partition at running_addr (we really are in its app slot);
// `ours` has ota_0 at running_addr, big enough for image_len; `ours` has nvs + otadata. Byte-identical tables
// -> NONE. `why` gets a one-line reason either way.
ha_partmig_verdict_t ha_partmig_plan(const uint8_t *cur, size_t cur_len, const uint8_t *ours, size_t ours_len,
                                     uint32_t running_addr, uint32_t image_len, char *why, size_t why_sz);

// IDF executor: reads the live table, plans, and if MIGRATE performs steps 1–4 above (restarts; never
// returns). Call FIRST in app_main, before nvs_flash_init(). Returns the verdict when it does not migrate.
// `log` (nullable) receives the reason line.
ha_partmig_verdict_t ha_partmig_run(const uint8_t *ours, size_t ours_len, void (*log)(const char *fmt, ...));
