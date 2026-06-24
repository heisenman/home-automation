// WS2812 operability LED — see ha_led.h for the philosophy + code table. Dependency-free: drives the
// single onboard addressable LED with the ESP-IDF RMT TX peripheral (no managed component to fetch, so
// the build stays air-gap reproducible). RMT has its own peripheral block — no contention with the
// 2.4 GHz radio the BLE/Wi-Fi stack shares.
#include "ha_led.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/rmt_tx.h"
#include "esp_log.h"

static const char *TAG = "ha_led";

// Waveshare ESP32-S3-ETH onboard WS2812 RGB (confirmed GPIO21; clear of the W5500 pins 9-14 and the
// radio). The industrial 8DI-8DO/8RO variants differ — change this one #define if the LED stays dark.
#define LED_GPIO     21
#define RMT_RES_HZ   10000000      // 10 MHz -> 0.1 us / tick, for the WS2812 bit timings below

static rmt_channel_handle_t s_chan;
static rmt_encoder_handle_t s_enc;
static volatile ha_led_state_t s_state = HA_LED_OK;

// Push one pixel. WS2812 wire order is G,R,B.
static void led_rgb(uint8_t r, uint8_t g, uint8_t b) {
    if (!s_chan || !s_enc) return;
    uint8_t grb[3] = { g, r, b };
    rmt_transmit_config_t tx = { .loop_count = 0 };
    rmt_transmit(s_chan, s_enc, grb, sizeof(grb), &tx);
    rmt_tx_wait_all_done(s_chan, 100);
}
static inline void led_off(void) { led_rgb(0, 0, 0); }

// Dim on purpose — these are indicators, not a flashlight (and easy on the eye at night).
#define C_RED      40, 0, 0
#define C_AMBER    40, 14, 0
#define C_BLUE     0, 0, 40
#define C_MAGENTA  40, 0, 40

// Blink n times (~1 s on / 1 s off) then a ~4 s gap. Bails immediately if the state changed, so the
// LED reacts to recovery within ~1 s instead of finishing a long pattern.
static void blink(ha_led_state_t self, int n, uint8_t r, uint8_t g, uint8_t b) {
    for (int i = 0; i < n && s_state == self; i++) {
        led_rgb(r, g, b); vTaskDelay(pdMS_TO_TICKS(1000));
        led_off();        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    for (int t = 0; t < 8 && s_state == self; t++) vTaskDelay(pdMS_TO_TICKS(500)); // ~4 s gap, interruptible
}

static void led_task(void *arg) {
    for (;;) {
        switch (s_state) {
            case HA_LED_FATAL:     led_rgb(C_RED); vTaskDelay(pdMS_TO_TICKS(1000)); break; // solid
            case HA_LED_NET_DOWN:  blink(HA_LED_NET_DOWN,  2, C_RED);     break;
            case HA_LED_WIFI_DOWN: blink(HA_LED_WIFI_DOWN, 3, C_AMBER);   break;
            case HA_LED_MQTT_DOWN: blink(HA_LED_MQTT_DOWN, 4, C_BLUE);    break;
            case HA_LED_OTA_FAIL:  blink(HA_LED_OTA_FAIL,  5, C_MAGENTA); break;
            case HA_LED_OK:
            default:               led_off(); vTaskDelay(pdMS_TO_TICKS(500)); break;
        }
    }
}

void ha_led_init(void) {
    rmt_tx_channel_config_t cc = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .gpio_num = LED_GPIO,
        .mem_block_symbols = 64,
        .resolution_hz = RMT_RES_HZ,
        .trans_queue_depth = 4,
    };
    if (rmt_new_tx_channel(&cc, &s_chan) != ESP_OK) {
        ESP_LOGW(TAG, "RMT channel init failed — operability LED disabled (node still relays)");
        return;
    }
    rmt_bytes_encoder_config_t ec = {
        .bit0 = { .level0 = 1, .duration0 = 3, .level1 = 0, .duration1 = 9 },   // 0.3 us H / 0.9 us L
        .bit1 = { .level0 = 1, .duration0 = 9, .level1 = 0, .duration1 = 3 },   // 0.9 us H / 0.3 us L
        .flags = { .msb_first = 1 },
    };
    if (rmt_new_bytes_encoder(&ec, &s_enc) != ESP_OK) {
        ESP_LOGW(TAG, "RMT encoder init failed — operability LED disabled");
        return;
    }
    rmt_enable(s_chan);
    led_off();
    xTaskCreate(led_task, "ha_led", 2560, NULL, 3, NULL);
    ESP_LOGI(TAG, "operability LED up (WS2812 GPIO%d) — OFF when healthy, lights only on fault", LED_GPIO);
}

void ha_led_set(ha_led_state_t state) {
    if (s_state == HA_LED_FATAL) return; // terminal config fault — only a re-enroll + reflash clears it,
                                         // so a later OK/MQTT-up must not silence a mis-enrolled node
    s_state = state;                     // latest-wins; the renderer task picks it up on its next loop
}
