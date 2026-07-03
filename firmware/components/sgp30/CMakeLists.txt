# SGP30 eCO2 + TVOC gas sensor — minimal I2C driver (new i2c_master API). Pure transport, no node policy.
# Unlike the SGP40 (raw SRAW -> `sensirion_gas_index`), the SGP30 computes eCO2/TVOC on-chip, so NO
# companion algorithm component is needed. Board wiring (bus, pins, SCL speed) is injected by the caller.
# Reusable by any device with an I2C bus.
idf_component_register(SRCS "sgp30.c"
                       INCLUDE_DIRS "include"
                       REQUIRES driver)
