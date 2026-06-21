#pragma once
#include <stdbool.h>
#include "switchbot_decode.h"

// Start the MQTT client (with a retained LWT on home/edge/<node>/status = "offline").
void ha_mqtt_start(const char *broker_uri, const char *node_id);
bool ha_mqtt_is_connected(void);

// Publish a decoded reading keyed by MAC to home/edge/<node>/<mac>/adv.
// mac_str is "AA:BB:CC:DD:EE:FF"; rssi in dBm.
void ha_mqtt_publish_reading(const char *mac_str, const sb_reading_t *r, int rssi);
