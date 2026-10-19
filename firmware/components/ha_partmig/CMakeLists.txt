# ha_partmig: one-time, in-place partition-table migration off a foreign OTA layout (Tasmota "safeboot").
# The planner (ha_partmig_plan.c) is pure C and host-tested (test/run.sh); ha_partmig.c is the IDF executor.
# The node must build with CONFIG_SPI_FLASH_DANGEROUS_WRITE_ALLOWED=y or the table write aborts.
idf_component_register(SRCS "ha_partmig_plan.c" "ha_partmig.c"
                       INCLUDE_DIRS "include"
                       REQUIRES spi_flash app_update esp_partition bootloader_support)
