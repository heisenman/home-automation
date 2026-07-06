// Node config loader (ADR-0020) — see ha_config.h. Promoted from edge/esp32c6/main/ha_config.c; the only
// change vs the fork is the secrets seam: compile-time defaults arrive as a parameter (app_main reads the
// board-local secrets.h and passes them), so this shared component never includes secrets. NVS overlay +
// provisioning precedence are unchanged.
#include "ha_config.h"
#include <string.h>
#include <stdio.h>
#include "nvs.h"
#include "esp_log.h"

static const char *TAG = "ha_config";

// Overlay one string key from NVS namespace "ha" if present.
static void nvs_overlay(nvs_handle_t h, const char *key, char *dst, size_t dst_sz) {
    size_t len = dst_sz;
    if (nvs_get_str(h, key, dst, &len) == ESP_OK) {
        ESP_LOGI(TAG, "config[%s] from NVS", key);
    }
}

void ha_config_load(ha_config_t *cfg, const ha_config_t *defaults) {
    // 1) compile-time defaults (board's secrets.h, passed by app_main)
    *cfg = *defaults;

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
