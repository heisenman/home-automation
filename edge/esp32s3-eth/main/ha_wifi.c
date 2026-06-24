#include "ha_wifi.h"
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_log.h"
#include "ha_led.h"

static const char *TAG = "ha_wifi";
static EventGroupHandle_t s_wifi_events;
#define WIFI_CONNECTED_BIT BIT0

// An edge relay must STAY online on a flaky AP. The inherited C6 driver gave up after 20 retries →
// permanent offline on repeated beacon timeouts. Two robustness rules:
//   1. Reconnect FOREVER on every disconnect (no retry cap).
//   2. Down-watchdog: if there's no IP for WIFI_DOWN_REBOOT_MS, reboot — app_main then re-runs the
//      auto-sense (Ethernet-first), so a cable plugged in *during* a Wi-Fi outage is picked up too.
#define WIFI_DOWN_REBOOT_MS 120000
static esp_timer_handle_t s_down_wd;

static void wd_reboot_cb(void *arg) {
    ESP_LOGE(TAG, "Wi-Fi down > %d ms — rebooting to recover (and re-check for an Ethernet cable)",
             WIFI_DOWN_REBOOT_MS);
    esp_restart();
}

static void on_wifi(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "disconnected — reconnecting");
        ha_led_set(HA_LED_WIFI_DOWN);                           // Wi-Fi link down → AMBER x3
        esp_wifi_connect();                                     // retry forever, no cap
        if (s_down_wd && !esp_timer_is_active(s_down_wd))       // arm the down-watchdog
            esp_timer_start_once(s_down_wd, (int64_t)WIFI_DOWN_REBOOT_MS * 1000);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "got ip " IPSTR, IP2STR(&e->ip_info.ip));
        ha_led_set(HA_LED_MQTT_DOWN);                           // link back; broker pending → mqtt sets OK
        if (s_down_wd && esp_timer_is_active(s_down_wd))
            esp_timer_stop(s_down_wd);                          // recovered — cancel the pending reboot
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
    }
}

esp_err_t ha_wifi_connect(const char *ssid, const char *psk, int timeout_ms) {
    s_wifi_events = xEventGroupCreate();
    // esp_netif_init() + esp_event_loop_create_default() are done once in app_main (shared with eth).
    esp_netif_create_default_wifi_sta();

    const esp_timer_create_args_t wd = {.callback = wd_reboot_cb, .name = "wifi_down_wd"};
    ESP_ERROR_CHECK(esp_timer_create(&wd, &s_down_wd));

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi, NULL, NULL));

    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, ssid, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, psk, sizeof(wc.sta.password));
    wc.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());

    EventBits_t bits = xEventGroupWaitBits(s_wifi_events, WIFI_CONNECTED_BIT,
                                           pdFALSE, pdFALSE, pdMS_TO_TICKS(timeout_ms));
    return (bits & WIFI_CONNECTED_BIT) ? ESP_OK : ESP_FAIL;
}
