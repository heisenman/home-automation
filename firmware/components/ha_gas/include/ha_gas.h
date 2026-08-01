// BREADCRUMB: firmware/components > ha_gas - node-local gas lane: picks the fitted chip (SGP40/SGP41/SGP30/BME680) at runtime — probing the bus when told AUTO — runs its sampling loop and publishes on the shared node-sensor path, so one generic image serves any sensor. Contract: ADR-0020. Parent: firmware/AGENTS.md.
// REUSE-WHEN: a node has (or might have) a gas sensor on I2C and you want it sampled and published without the image needing to know which part is soldered.
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
    HA_GAS_SENSOR_SGP41,       // Sensirion SGP41 VOC index + NOx index (0x59 — SAME ADDRESS as the SGP40)
    HA_GAS_SENSOR_SGP30,       // Sensirion SGP30 eCO2 + TVOC (0x58)
    HA_GAS_SENSOR_BME680,      // Bosch BME680 T/RH/P + gas resistance (0x76/0x77)
    HA_GAS_SENSOR_AUTO,        // probe the bus and pick — see ha_gas_start / ha_gas_detect
    HA_GAS_SENSOR_NONE,        // no gas lane on this node (relay-only); ha_gas_start returns immediately
} ha_gas_sensor_t;

// Probe the I2C bus and return which supported gas chip is present, or HA_GAS_SENSOR_NONE if none ACKs.
//
// Address alone is NOT enough. The Sensirion SGP40 and SGP41 share I2C address 0x59 — the SGP41 is a
// pin- and address-compatible upgrade that adds a NOx pixel — so a bus scan sees one address for two
// different command sets, and a module's silkscreen or product listing is not reliable either. When
// 0x59 ACKs, this asks the part itself which it is (sgp4x_identify), rather than assuming SGP40 as it
// did before. The other families are unambiguous by address (SGP30 0x58, BME680 0x76/0x77).
//
// Safe to call before ha_gas_start; it leaves the bus deinitialised.
ha_gas_sensor_t ha_gas_detect(int sda_gpio, int scl_gpio);

// Map to/from the short names used on the wire and in NVS ("sgp40"/"sgp41"/"sgp30"/"bme680"/"auto"/"none").
// ha_gas_from_name returns HA_GAS_SENSOR_AUTO for NULL/""/unrecognised — the safe default, because an
// unknown string must not silently select the wrong driver.
ha_gas_sensor_t ha_gas_from_name(const char *name);
const char *ha_gas_name(ha_gas_sensor_t s);
// The device_type this family publishes under ("sgp40_gas" etc.), or NULL for AUTO/NONE. Used for the
// ADR-0036 `hello` abilities list, so a node advertises what it ACTUALLY found, not what it was told.
const char *ha_gas_device_type(ha_gas_sensor_t s);

// sda_gpio/scl_gpio are the board's I2C pads for the gas sensor (board-specific — e.g. XIAO C6 = 22/23,
// Waveshare S3-ETH = 42/41; GPIO22/23 don't exist on the S3). No-op if the sensor isn't wired/present.
// Pass HA_GAS_SENSOR_AUTO to probe-and-pick (ADR-0036 generic image: one build serves any sensor).
void ha_gas_start(const char *node_id, ha_gas_sensor_t sensor, int sda_gpio, int scl_gpio);

// Which sensor ha_gas_start actually ended up running (resolved from AUTO), or NONE if the lane is down.
ha_gas_sensor_t ha_gas_active(void);
