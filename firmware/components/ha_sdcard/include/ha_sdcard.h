// BREADCRUMB: firmware/components > ha_sdcard - microSD mount over SDMMC+FAT, board power/slot/width/freq as config; hot-plug presence. Contract: ADR-0020. Parent: firmware/AGENTS.md.
// REUSE-WHEN: a device needs mounted microSD storage that adapts to new boards without forking
//
// SD card mount (ADR-0020).
//
// Brings up a microSD over SDMMC and mounts a FAT filesystem, with the board-specific bits
// (card VDD power switch, on-chip LDO channel, slot/width/freq) as config so future nodes
// adapt without forking. On the reTerminal D1001 the card shares the P4's single SDMMC host
// with the C6's SDIO link (esp-hosted, slot 1) — this mounts slot 0 alongside it.
//
// CRITICAL (D1001): the card VDD is gated behind a GPIO (BSP_SD_PWR_EN); without driving it
// high the card never responds ("sdmmc_req: handle_idle_state_events"). Set pwr_en_gpio.
#pragma once
#include "esp_err.h"
#include <stdbool.h>
#include <stdint.h>

typedef struct {
    const char *mount_point;        // default "/sdcard"
    int  pwr_en_gpio;               // card VDD switch GPIO, -1 if none (D1001: 46)
    int  ldo_chan_id;               // on-chip LDO channel feeding the SDMMC IO rail (D1001: 4)
    int  slot;                      // SDMMC host slot (D1001: 0; the C6 SDIO is slot 1)
    int  width;                     // bus width 1/4 (D1001: 4)
    int  max_freq_khz;              // e.g. SDMMC_FREQ_HIGHSPEED (40000)
    bool format_if_mount_failed;    // format a blank/foreign card to FAT
} ha_sdcard_cfg_t;

// Mount the card. Pass NULL for the reTerminal D1001 defaults (/sdcard, pwr GPIO46, LDO ch4,
// slot 0, 4-bit, high-speed, format-if-needed). Idempotent: a second call is a no-op if
// already mounted. Non-fatal by design — the caller decides what to do on failure.
esp_err_t ha_sdcard_mount(const ha_sdcard_cfg_t *cfg);

bool        ha_sdcard_mounted(void);       // true once mounted
const char *ha_sdcard_mount_point(void);   // the active mount point (e.g. "/sdcard")
uint64_t    ha_sdcard_size_mb(void);        // card capacity in MB (0 if unmounted)

// Presence global (== mounted+usable). The authority other modules gate on to avoid touching a
// pulled card. Maintained by ha_sdcard_watch() when a card-detect line is supplied.
bool ha_sdcard_present(void);

// Unmount the card. Safe: consumers must presence-gate (nothing may hold an open handle across
// this). The stored host carries a no-op deinit, so the shared SDMMC host is left untouched.
void ha_sdcard_unmount(void);

// Start a hot-plug watcher on the card-detect line (both-edge ISR + 3 s self-heal poll). On
// insert it mounts `cfg` (NULL = D1001 defaults) and, after a *successful* mount, calls
// on_change(true); on removal it unmounts and calls on_change(false). `active_low` = the detect
// pin reads 0 when a card is present (D1001 SD_DETECT = GPIO45). `on_change` may be NULL. Runs
// its own task; idempotent per boot. The current state is announced once at startup.
void ha_sdcard_watch(const ha_sdcard_cfg_t *cfg, int detect_gpio, bool active_low,
                     void (*on_change)(bool present));
