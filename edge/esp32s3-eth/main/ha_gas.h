#pragma once
// SGP-40 VOC gas lane — a dual-role add-on that runs ALONGSIDE the BLE relay + GATT central on the
// S3-ETH node. I2C is a separate peripheral block from the 2.4 GHz radio, so the 1 Hz gas sampling
// keeps running right through a passive-scan pause / GATT connect — no radio contention.
//
// No-op-safe: if the sensor isn't found on I2C (not wired / bad solder), it logs the reason over MQTT
// and returns — the node keeps relaying BLE + serving GATT either way. Call once, after ha_mqtt_start().
//
// Publishes on the shared node-local-sensor path (ha_mqtt_publish_node_sensor) so the dictator's
// existing edge_mapper resolves it via the registry with no new server ingest path.
void ha_gas_start(void);
