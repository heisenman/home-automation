#pragma once
// Operability LED for the edge node. Philosophy (Hugh): the eye only needs the LED when something is
// WRONG — so it is OFF when healthy and lights ONLY on a fault, with a slow, long, human-countable
// pattern (color = category, blink-count = code). Humans + eyeballs are slow, so blinks are ~1 s on /
// 1 s off with a ~4 s gap between repeats. The published code table lives in edge/FIRMWARE-GUIDE.md §6.
// Drives the onboard WS2812 RGB (Waveshare ESP32-S3-ETH: GPIO21) via RMT — no external dependency.

typedef enum {
    HA_LED_OK = 0,     // relaying normally                                  -> LED OFF
    HA_LED_FATAL,      // config invalid (no command secret / un-enrolled)   -> RED solid
    HA_LED_NET_DOWN,   // no network at all (neither Ethernet nor Wi-Fi)     -> RED     x2
    HA_LED_WIFI_DOWN,  // Wi-Fi link down, reconnecting                      -> AMBER   x3
    HA_LED_MQTT_DOWN,  // network up but broker unreachable                  -> BLUE    x4
    HA_LED_OTA_FAIL,   // OTA failed / rolled back                           -> MAGENTA x5
} ha_led_state_t;

// Bring up the WS2812 driver (LED starts OFF) and spawn the renderer task. Call once at boot; if the
// RMT peripheral can't init, it logs and no-ops (the node still relays — the LED is operability, not load-bearing).
void ha_led_init(void);

// Report the node's current health. Latest-wins, thread-safe. HA_LED_OK turns the LED off.
void ha_led_set(ha_led_state_t state);
