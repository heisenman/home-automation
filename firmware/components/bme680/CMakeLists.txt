# BME680 environmental + gas sensor — minimal I2C driver with the Bosch compensation algorithm baked in
# (raw ADC + on-chip calibration -> °C / %RH / hPa / Ω, gas via the two Bosch range LUTs). Pure
# transport + math, no node policy; board wiring (bus, pins, SCL speed, addr) is injected by the caller.
idf_component_register(SRCS "bme680.c"
                       INCLUDE_DIRS "include"
                       REQUIRES driver)
