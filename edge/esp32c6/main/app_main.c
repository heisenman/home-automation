// ESP32-C6 edge node: passive SwitchBot BLE scanner that relays decoded readings to the
// dictator's MQTT broker. Foundational native-C firmware (ADR-0003 Wasm host is Phase 8).
#include "nvs_flash.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "ha_config.h"
#include "ha_wifi.h"
#include "ha_sntp.h"
#include "ha_mqtt.h"
#include "ble_scan.h"

static const char *TAG = "ha_edge";

void app_main(void) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    ha_config_t cfg;
    ha_config_load(&cfg);

    if (ha_wifi_connect(cfg.wifi_ssid, cfg.wifi_psk, 30000) != ESP_OK) {
        ESP_LOGE(TAG, "Wi-Fi connect failed — restarting in 10s");
        vTaskDelay(pdMS_TO_TICKS(10000));
        esp_restart();
    }

    if (!ha_sntp_sync(cfg.ntp_server, 15000)) {
        ESP_LOGW(TAG, "SNTP not synced — readings ship without ts; mapper stamps on ingest");
    }

    ha_mqtt_start(cfg.broker_uri, cfg.node_id);
    ha_ble_scan_start();
    ESP_LOGI(TAG, "edge node up: node=%s broker=%s", cfg.node_id, cfg.broker_uri);
}
