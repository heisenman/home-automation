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
#include "ha_ota.h"
#include "ha_relay.h"
#include "ha_ble_scan.h"

// ha_config secrets seam (ADR-0020): the shared ha_config component stays secrets-free; app_main reads the
// board-local secrets.h and passes the compile-time defaults in (NVS then overlays them in production).
#if __has_include("secrets.h")
#include "secrets.h"
#else
#warning "secrets.h not found — copy secrets.example.h to secrets.h and fill it in (or provision NVS)."
#define HA_WIFI_SSID  ""
#define HA_WIFI_PSK   ""
#define HA_BROKER_URI "mqtt://192.168.0.245:1883"
#define HA_NODE_ID    "c6-bench"
#define HA_NTP_SERVER "pool.ntp.org"
#endif
// ha_mqtt seam: secrets.h provides these in production; fall back for a bench build. HA_FW_VERSION is board-set.
#ifndef HA_CMD_SECRET
#define HA_CMD_SECRET ""
#endif
#ifndef HA_OTA_HOST
#define HA_OTA_HOST "192.168.0.245"
#endif
#ifndef HA_MQTT_USER
#define HA_MQTT_USER ""
#endif
#ifndef HA_MQTT_PASS
#define HA_MQTT_PASS ""
#endif
#ifndef HA_FW_VERSION
#define HA_FW_VERSION "v11-modular"
#endif

static const char *TAG = "ha_edge";

// Edge publish sink for the shared ha_ble_scan observer (ADR-0020): Phase-B relay gate,
// then publish. Replaces the tail of the old fork ble_scan.c gap_event now that
// parse/decode/dedup live in the shared component. controller_init stays NULL (native).
static void edge_on_reading(const char *mac_str, const sb_reading_t *r, int rssi, void *user) {
    (void)user;
    if (ha_relay_allowed(mac_str))
        ha_mqtt_publish_reading(mac_str, r, rssi);
}

void app_main(void) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    // STATIC, not a stack local — ha_mqtt/ha_ota keep POINTERS into this struct, so once app_main returns
    // a stack cfg is freed and they read garbage (the C6 and S3 forks both hit this: the OTA gate saw
    // node_id "unknown" and rejected every OTA). This fork still had the stack version; ADR-0036 Layer 0
    // makes it acute, because cmd_secret is now aliased the same way.
    static ha_config_t cfg;
    ha_config_load(&cfg, &(ha_config_t){ .wifi_ssid = HA_WIFI_SSID, .wifi_psk = HA_WIFI_PSK,
        .broker_uri = HA_BROKER_URI, .node_id = HA_NODE_ID, .ntp_server = HA_NTP_SERVER,
        .cmd_secret = HA_CMD_SECRET });

    if (ha_wifi_connect(cfg.wifi_ssid, cfg.wifi_psk, 30000) != ESP_OK) {
        ESP_LOGE(TAG, "Wi-Fi connect failed — restarting in 10s");
        vTaskDelay(pdMS_TO_TICKS(10000));
        esp_restart();
    }

    // ADR-0036 Layer 0 — mint this node's own command secret if it doesn't have one. AFTER Wi-Fi (the HW
    // RNG needs the radio up), BEFORE ha_mqtt_init. No-op if it already has one.
    if (!ha_config_ensure_node_secret(&cfg))
        ESP_LOGE(TAG, "no command secret — node will reject every signed command (incl. OTA)");

    if (!ha_sntp_sync(cfg.ntp_server, 15000)) {
        ESP_LOGW(TAG, "SNTP not synced — readings ship without ts; mapper stamps on ingest");
    }
    ha_sntp_start_periodic(30 * 60 * 1000);   // re-sync every 30 min (the C6 RTC drifts fast)

    ha_relay_init();                // load the persisted Phase-B coverage allowlist (default: relay-all)
    // cfg.cmd_secret, NOT HA_CMD_SECRET — NVS-first (node-born/provisioned), compile-time fallback.
    ha_mqtt_init(&(ha_mqtt_cfg_t){ .cmd_secret = cfg.cmd_secret, .ota_host = HA_OTA_HOST,
        .mqtt_user = HA_MQTT_USER, .mqtt_pass = HA_MQTT_PASS, .fw_version = HA_FW_VERSION,
        .enable_reach = false });   // c3 doesn't wire ha_reach
    ha_mqtt_start(cfg.broker_uri, cfg.node_id);
    ha_ble_scan_cfg_t scan_cfg = {
        .controller_init = NULL,          // native controller (nimble_port_init brings it up)
        .on_reading      = edge_on_reading,
        .shared_radio    = false,         // preserve pre-migration continuous scan (matches live c6)
        .user            = NULL,
    };
    ha_ble_scan_start(&scan_cfg);
    ESP_LOGI(TAG, "edge node up: node=%s broker=%s", cfg.node_id, cfg.broker_uri);

    // If we just booted a freshly-OTA'd image, self-test now and confirm-or-rollback.
    ha_ota_confirm_if_pending();
}
