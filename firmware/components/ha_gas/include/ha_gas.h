#pragma once

// Node-local gas lane — a dual-role add-on that runs ALONGSIDE the BLE relay (+ GATT central on the S3).
// I2C is a separate peripheral block from the 2.4 GHz radio, so 1 Hz sampling keeps running through a
// passive-scan pause / GATT connect — no radio contention.
//
// No-op-safe: if the sensor isn't found on I2C (not wired / bad solder), it logs the reason over MQTT and
// returns — the node keeps relaying either way. Call once, after ha_mqtt_start().
//
// Publishes on the shared node-local-sensor path (ha_mqtt_publish_node_sensor) so the dictator's existing
// edge_mapper resolves it via the registry (payload "mac" = "<node_id>-gas") with no new server ingest path.
//
// Which sensor is soldered is a per-node compile-time choice (secrets.h define); app_main — which CAN see
// secrets.h — resolves it to this enum and passes it, so this shared component needs no secrets.h (ADR-0020).
// NB: names are HA_GAS_SENSOR_* (not HA_GAS_*) so they don't collide with the empty `#define HA_GAS_SGP30`
// / `HA_GAS_BME680` compile-select flags a node's secrets.h uses to pick which one is soldered.
typedef enum {
    HA_GAS_SENSOR_SGP40 = 0,   // Sensirion SGP40 VOC index (0x59) — the default
    HA_GAS_SENSOR_SGP30,       // Sensirion SGP30 eCO2 + TVOC (0x58)
    HA_GAS_SENSOR_BME680,      // Bosch BME680 T/RH/P + gas resistance (0x76/0x77)
} ha_gas_sensor_t;

void ha_gas_start(const char *node_id, ha_gas_sensor_t sensor);
