#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "nimble/ble.h"
#include "host/ble_gap.h"

// Initialise the NimBLE host and start a passive BLE scan. shared_radio=true (node on Wi-Fi, sharing the
// one 2.4GHz radio) → duty-cycle the scan so Wi-Fi stays up; false (wired Ethernet) → continuous scan for
// maximum advert capture. Decoded SwitchBot readings are published via ha_mqtt_publish_reading().
void ha_ble_scan_start(bool shared_radio);

// Pause/resume the passive scan (for a GATT history pull on the single radio).
void ha_ble_scan_pause(void);
void ha_ble_scan_resume(void);

// own address type inferred at sync (for ble_gap_connect).
uint8_t ha_ble_own_addr_type(void);

// Look up the full BLE address (type + value) the scanner last saw for mac_str
// ("AA:BB:CC:DD:EE:FF"). Returns false if not seen. GATT connect needs the addr type.
bool ha_ble_lookup_addr(const char *mac_str, ble_addr_t *out);
