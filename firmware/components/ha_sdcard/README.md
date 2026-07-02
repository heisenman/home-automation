# ha_sdcard — microSD mount (ADR-0020)

Mounts a microSD (SDMMC + FAT) with the board-specific bits — card VDD power GPIO, on-chip
LDO channel, slot/width/freq — as `ha_sdcard_cfg_t` config so nodes adapt without forking.
Pass NULL for reTerminal D1001 defaults.

## Platform support
ESP32-P4 SDMMC (D1001 defaults: slot 0, LDO ch4, VDD GPIO46, 4-bit, high-speed). On the
D1001 the card shares the one P4 SDMMC host with the C6's SDIO link (esp-hosted on slot 1) —
this mounts slot 0 alongside it ("host already initialized, skipping init flow").

## Consumed by
- d1001-panel — battery profiler (`bat_profile`) + file-ops (`fs_ops`) both use the mount.
