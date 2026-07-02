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
