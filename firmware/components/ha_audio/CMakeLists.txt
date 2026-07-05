# ha_audio: shared audible-alert output (ADR-0020, roadmap #6). I2S-std TX + the vendored espressif__es8311
# driver (es8311/, adapted to the new i2c_master driver so it shares the panel's bsp_i2c1 bus). The amp
# enable is a caller callback, so this needs neither the io-expander nor any panel-specific dep — just the
# audio + i2c drivers. Public API = include/ha_audio.h; the es8311 headers stay private.
idf_component_register(SRCS "ha_audio.c" "es8311/es8311.c"
                       INCLUDE_DIRS "include"
                       PRIV_INCLUDE_DIRS "es8311"
                       REQUIRES driver
                       PRIV_REQUIRES esp_driver_i2s esp_driver_i2c)
