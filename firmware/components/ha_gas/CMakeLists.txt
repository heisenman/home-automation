# Node-local gas lane (SGP40 / SGP41 / SGP30 / BME680), selected at RUNTIME via ha_gas_start(node_id, sensor, sda, scl)
# so the component needs no per-node secrets.h (ADR-0020). All four sensor drivers are linked (small); the
# caller passes which one is soldered, or AUTO to probe. Publishes via ha_mqtt's node-local-sensor path.
idf_component_register(SRCS "ha_gas.c"
                       INCLUDE_DIRS "include"
                       REQUIRES driver esp_driver_i2c nvs_flash ha_mqtt sgp40 sgp41 sgp30 bme680 sensirion_gas_index)
