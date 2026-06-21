#pragma once
// Initialise the NimBLE host and start a continuous passive BLE scan.
// Decoded SwitchBot readings are published via ha_mqtt_publish_reading().
void ha_ble_scan_start(void);
