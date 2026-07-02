# microSD mount over SDMMC + FAT. Board-specific pins/power are config (ha_sdcard_cfg_t),
# defaulting to the reTerminal D1001 (slot 0, LDO ch4, VDD GPIO46). esp_driver_sdmmc carries
# the on-chip-LDO power-control helper; fatfs provides the VFS mount.
idf_component_register(SRCS "ha_sdcard.c"
                       INCLUDE_DIRS "include"
                       REQUIRES fatfs sdmmc esp_driver_sdmmc driver)
