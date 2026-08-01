# SGP41 VOC+NOx gas sensor — minimal I2C driver (new i2c_master API). Pure transport, no node policy;
# pair with the `sensirion_gas_index` component twice (VOC + NOx) to turn the two raw signals into
# indices. Board wiring (bus, pins, SCL speed) is injected by the caller. Also owns sgp4x_identify(),
# which tells an SGP41 apart from an SGP40 — they share I2C address 0x59, so a bus scan cannot.
idf_component_register(SRCS "sgp41.c"
                       INCLUDE_DIRS "include"
                       REQUIRES driver)
