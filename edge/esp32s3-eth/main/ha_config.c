#include "ha_config.h"
#include <string.h>
#include "nvs.h"
#include "esp_log.h"

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

static const char *TAG = "ha_config";

// Overlay one string key from NVS namespace "ha" if present.
static void nvs_overlay(nvs_handle_t h, const char *key, char *dst, size_t dst_sz) {
    size_t len = dst_sz;
    if (nvs_get_str(h, key, dst, &len) == ESP_OK) {
        ESP_LOGI(TAG, "config[%s] from NVS", key);
    }
}

void ha_config_load(ha_config_t *cfg) {
    // 1) compile-time defaults
    snprintf(cfg->wifi_ssid, sizeof(cfg->wifi_ssid), "%s", HA_WIFI_SSID);
    snprintf(cfg->wifi_psk, sizeof(cfg->wifi_psk), "%s", HA_WIFI_PSK);
    snprintf(cfg->broker_uri, sizeof(cfg->broker_uri), "%s", HA_BROKER_URI);
    snprintf(cfg->node_id, sizeof(cfg->node_id), "%s", HA_NODE_ID);
    snprintf(cfg->ntp_server, sizeof(cfg->ntp_server), "%s", HA_NTP_SERVER);

    // 2) NVS overlay (production provisioning) — best-effort
    nvs_handle_t h;
    if (nvs_open("ha", NVS_READONLY, &h) == ESP_OK) {
        nvs_overlay(h, "wifi_ssid", cfg->wifi_ssid, sizeof(cfg->wifi_ssid));
        nvs_overlay(h, "wifi_psk", cfg->wifi_psk, sizeof(cfg->wifi_psk));
        nvs_overlay(h, "broker_uri", cfg->broker_uri, sizeof(cfg->broker_uri));
        nvs_overlay(h, "node_id", cfg->node_id, sizeof(cfg->node_id));
        nvs_overlay(h, "ntp_server", cfg->ntp_server, sizeof(cfg->ntp_server));
        nvs_close(h);
    }
    ESP_LOGI(TAG, "node=%s broker=%s ntp=%s ssid=%s",
             cfg->node_id, cfg->broker_uri, cfg->ntp_server, cfg->wifi_ssid);
}
