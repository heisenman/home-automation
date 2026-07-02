# Battery gauge (ADC→SoC LUT) + IMU board-temp + thermal-gated charge manager with a
# restart watchdog. All board wiring (ADC channels, PCA9535 charge/read-enable pins, the
# IMU + its I2C bus, status GPIOs, LUT, thresholds) is ha_battery_cfg_t; ha_battery_d1001_cfg()
# is the reTerminal D1001 preset. The expander + I2C bus handles are injected by the caller
# (the panel's display BSP owns them).
idf_component_register(SRCS "ha_battery.c"
                       INCLUDE_DIRS "include"
                       REQUIRES esp_adc driver esp_io_expander esp_timer)
