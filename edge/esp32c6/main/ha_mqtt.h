#pragma once
#include <stdbool.h>
#include "switchbot_decode.h"

// Start the MQTT client (with a retained LWT on home/edge/<node>/status = "offline").
void ha_mqtt_start(const char *broker_uri, const char *node_id);
bool ha_mqtt_is_connected(void);

// Publish a decoded reading keyed by MAC to home/edge/<node>/<mac>/adv.
// mac_str is "AA:BB:CC:DD:EE:FF"; rssi in dBm.
void ha_mqtt_publish_reading(const char *mac_str, const sb_reading_t *r, int rssi);

// Publish a raw history-relay message to home/edge/<node>/<mac>/history (qos 1).
void ha_mqtt_publish_history(const char *mac_str, const char *payload);

// Publish a generic GATT-forwarder reply to home/edge/<node>/<reqid>/reply (qos 1).
void ha_mqtt_publish_reply(const char *reqid, const char *payload);

// Publish a NODE-LOCAL (non-BLE) sensor reading to home/edge/<node>/<key>/adv (qos 1).
// Reusable path for on-node peripherals (I2C, ADC, ...): `key` is the topic segment, `reg_key` is the
// registry lookup key placed in the payload "mac" field, `metrics_json` is a JSON object literal.
// transport is tagged "i2c-local". Resolved by the dictator's edge_mapper like any BLE reading.
void ha_mqtt_publish_node_sensor(const char *key, const char *reg_key,
                                 const char *device_type, const char *metrics_json);

// Remote log line to home/edge/<node>/log (qos 0) — debug a headless node over MQTT.
void ha_mqtt_log(const char *fmt, ...);
