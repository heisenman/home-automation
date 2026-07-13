# ha_gas — node-local gas lane (shared, ADR-0020)

`ha_gas_start(node_id, sensor, sda_gpio, scl_gpio)` runs a gas sensor at 1 Hz (BME680 ~10 s) alongside the BLE relay and
publishes node-local readings via `ha_mqtt_publish_node_sensor` (registry key `<node_id>-gas`). No-op-safe:
a missing/mis-soldered sensor logs the reason over MQTT and returns; the relay keeps running.

Sensor is a RUNTIME parameter (`HA_GAS_SGP40` / `HA_GAS_SGP30` / `HA_GAS_BME680`) so the component needs no
`secrets.h` — app_main (which sees the per-node compile-select) resolves it and passes the enum. All three
drivers (`sgp40`+`sensirion_gas_index`, `sgp30`, `bme680`) are linked; only the selected path runs.
