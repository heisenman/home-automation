# Node-local gas lane (SGP40 / SGP30 / BME680), selected at RUNTIME via ha_gas_start(node_id, sensor) so the
# component needs no per-node secrets.h (ADR-0020). All three sensor drivers are linked (small); the caller
# passes which one is soldered. Publishes via ha_mqtt's node-local-sensor path.
idf_component_register(SRCS "ha_gas.c"
                       INCLUDE_DIRS "include"
                       REQUIRES driver esp_driver_i2c nvs_flash ha_mqtt sgp40 sgp30 bme680 sensirion_gas_index)
