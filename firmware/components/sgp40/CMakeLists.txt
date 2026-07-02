# SGP40 VOC gas sensor — minimal I2C driver (new i2c_master API). Pure transport, no node policy;
# pair with the `sensirion_gas_index` component to turn raw SRAW into a 0..500 VOC Index. Board wiring
# (bus, pins, SCL speed) is injected by the caller. Reusable by any device with an I2C bus.
idf_component_register(SRCS "sgp40.c"
                       INCLUDE_DIRS "include"
                       REQUIRES driver)
