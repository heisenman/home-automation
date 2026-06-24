// ESP32-S3-POE-ETH edge node: passive SwitchBot BLE scanner that relays decoded readings to the
// dictator's MQTT broker. PREFERS wired Ethernet (W5500); auto-falls back to onboard Wi-Fi when no
// cable is present. Transport switching is INTERRUPT-DRIVEN (the W5500 raises INT on a link change →
// ESP-IDF ETHERNET_EVENT → we reboot to re-pick) — no polling loop. Same relay contract as the C6
// node (home/edge/<node>/<mac>/adv).
#include "nvs_flash.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_eth.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "ha_config.h"
#include "ha_eth.h"
#include "ha_wifi.h"
#include "ha_sntp.h"
#include "ha_mqtt.h"
#include "ha_ota.h"
#include "ble_scan.h"

static const char *TAG = "ha_edge";

// Interrupt-driven transport switch: a reboot cleanly re-picks Ethernet-vs-Wi-Fi. The eth driver is
// left running even when no cable is present, so its CONNECTED interrupt can tell us one was plugged in.
static void on_eth_link_up(void *a, esp_event_base_t b, int32_t id, void *d) {
    ESP_LOGW(TAG, "Ethernet cable detected (link up) while on Wi-Fi — rebooting onto the wired link");
    esp_restart();
}
static void on_eth_link_down(void *a, esp_event_base_t b, int32_t id, void *d) {
    ESP_LOGW(TAG, "Ethernet link lost — rebooting to fall back to Wi-Fi");
    esp_restart();
}

void app_main(void) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    ha_config_t cfg;
    ha_config_load(&cfg);

    // Shared network init — exactly once; both transports attach to this stack + event loop.
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    // PREFER the wire: try Ethernet first (short wait — the W5500 link comes up fast with a cable in).
    bool net_ok = false;
    ESP_LOGI(TAG, "network: trying Ethernet (W5500) first...");
    if (ha_eth_connect(8000) == ESP_OK) {
        ESP_LOGI(TAG, "network: on Ethernet (wired)");
        // Cable pulled later → its DISCONNECTED interrupt reboots us onto Wi-Fi.
        ESP_ERROR_CHECK(esp_event_handler_register(ETH_EVENT, ETHERNET_EVENT_DISCONNECTED,
                                                   on_eth_link_down, NULL));
        net_ok = true;
    } else {
        ESP_LOGW(TAG, "network: no Ethernet link — coming up on Wi-Fi (%s)", cfg.wifi_ssid);
        if (ha_wifi_connect(cfg.wifi_ssid, cfg.wifi_psk, 30000) == ESP_OK) {
            ESP_LOGI(TAG, "network: on Wi-Fi");
            // Cable plugged in later → the W5500 link interrupt reboots us onto Ethernet (preferred).
            ESP_ERROR_CHECK(esp_event_handler_register(ETH_EVENT, ETHERNET_EVENT_CONNECTED,
                                                       on_eth_link_up, NULL));
            net_ok = true;
        }
    }
    if (!net_ok) {
        ESP_LOGE(TAG, "no network (neither Ethernet nor Wi-Fi) — restarting in 10s");
        vTaskDelay(pdMS_TO_TICKS(10000));
        esp_restart();
    }

    if (!ha_sntp_sync(cfg.ntp_server, 15000)) {
        ESP_LOGW(TAG, "SNTP not synced — readings ship without ts; mapper stamps on ingest");
    }
    ha_sntp_start_periodic(30 * 60 * 1000);   // re-sync every 30 min

    ha_mqtt_start(cfg.broker_uri, cfg.node_id);
    ha_ble_scan_start();
    ESP_LOGI(TAG, "edge node up: node=%s broker=%s", cfg.node_id, cfg.broker_uri);

    // If we just booted a freshly-OTA'd image, self-test now and confirm-or-rollback.
    ha_ota_confirm_if_pending();
}
