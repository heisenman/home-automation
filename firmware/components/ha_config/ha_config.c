// Node config loader (ADR-0020) — see ha_config.h. Promoted from edge/esp32c6/main/ha_config.c; the only
// change vs the fork is the secrets seam: compile-time defaults arrive as a parameter (app_main reads the
// board-local secrets.h and passes them), so this shared component never includes secrets. NVS overlay +
// provisioning precedence are unchanged.
#include "ha_config.h"
#include <string.h>
#include <stdio.h>
#include "nvs.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ha_config";

// Repoint boot-count revert (DJ-19): a separate NVS namespace holds the pending flag, the trial-boot
// counter, and a backup of the pre-repoint effective config. After RP_MAX_TRIES failed trial boots the
// node reverts to the backup and reboots — so a bad repoint recovers over the air, never a physical trip.
#define RP_NS        "ha_repoint"
#define RP_MAX_TRIES 3

// The effective config as last loaded, cached so ha_config_repoint_apply can back up a known-good state
// (including compile defaults for keys that weren't NVS-overlaid) before it writes the new values.
static ha_config_t s_effective;
static bool s_have_effective;

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
        nvs_overlay(h, "ota_host", cfg->ota_host, sizeof(cfg->ota_host));
        nvs_close(h);
    }
    s_effective = *cfg;          // cache for repoint backup
    s_have_effective = true;
    ESP_LOGI(TAG, "node=%s broker=%s ntp=%s ssid=%s ota_host=%s",
             cfg->node_id, cfg->broker_uri, cfg->ntp_server, cfg->wifi_ssid, cfg->ota_host);
}

bool ha_config_repoint_apply(const char *ssid, const char *psk, const char *broker,
                             const char *ntp, const char *ota_host) {
    if (!ssid || !ssid[0] || !psk || !broker || !broker[0]) {
        ESP_LOGE(TAG, "repoint: missing ssid/psk/broker — refused");
        return false;
    }
    // 1) Arm the revert: back up the current effective config + set pending, in one commit. If we have no
    //    cached effective config (shouldn't happen — load runs first), back up empties so a revert clears
    //    the overlay to compile defaults rather than restoring garbage.
    const ha_config_t *e = s_have_effective ? &s_effective : NULL;
    nvs_handle_t r;
    if (nvs_open(RP_NS, NVS_READWRITE, &r) != ESP_OK) { ESP_LOGE(TAG, "repoint: nvs open %s failed", RP_NS); return false; }
    nvs_set_str(r, "bk_ssid",   e ? e->wifi_ssid  : "");
    nvs_set_str(r, "bk_psk",    e ? e->wifi_psk   : "");
    nvs_set_str(r, "bk_broker", e ? e->broker_uri : "");
    nvs_set_str(r, "bk_ntp",    e ? e->ntp_server : "");
    nvs_set_str(r, "bk_host",   e ? e->ota_host   : "");
    nvs_set_u8(r, "pending", 1);
    nvs_set_u8(r, "tries", 0);
    if (nvs_commit(r) != ESP_OK) { nvs_close(r); ESP_LOGE(TAG, "repoint: arm commit failed"); return false; }
    nvs_close(r);

    // 2) Write the new network params into the "ha" overlay (node_id left untouched).
    nvs_handle_t h;
    esp_err_t err = nvs_open("ha", NVS_READWRITE, &h);
    if (err == ESP_OK) {
        nvs_set_str(h, "wifi_ssid", ssid);
        nvs_set_str(h, "wifi_psk", psk);
        nvs_set_str(h, "broker_uri", broker);
        if (ntp && ntp[0])           nvs_set_str(h, "ntp_server", ntp);
        if (ota_host && ota_host[0]) nvs_set_str(h, "ota_host", ota_host);
        err = nvs_commit(h);
        nvs_close(h);
    }
    if (err != ESP_OK) {
        // New config didn't land — disarm so we don't count pointless boots; node stays on old config.
        ESP_LOGE(TAG, "repoint: overlay write failed (%s) — disarming, not moving", esp_err_to_name(err));
        if (nvs_open(RP_NS, NVS_READWRITE, &r) == ESP_OK) { nvs_set_u8(r, "pending", 0); nvs_commit(r); nvs_close(r); }
        return false;
    }

    // 3) Reboot into the new config, armed for revert.
    ESP_LOGW(TAG, "repoint ARMED: ssid=%s broker=%s — rebooting (revert after %d failed boots)",
             ssid, broker, RP_MAX_TRIES);
    vTaskDelay(pdMS_TO_TICKS(500));   // let the log flush + the MQTT ack settle
    esp_restart();
    return true;                      // unreachable
}

void ha_config_repoint_boot_check(void) {
    nvs_handle_t r;
    if (nvs_open(RP_NS, NVS_READONLY, &r) != ESP_OK) return;   // namespace absent -> nothing pending
    uint8_t pending = 0, tries = 0;
    esp_err_t ep = nvs_get_u8(r, "pending", &pending);
    nvs_get_u8(r, "tries", &tries);
    char bk_ssid[33] = "", bk_psk[64] = "", bk_broker[96] = "", bk_ntp[64] = "", bk_host[64] = "";
    size_t n;
    n = sizeof(bk_ssid);   nvs_get_str(r, "bk_ssid",   bk_ssid,   &n);
    n = sizeof(bk_psk);    nvs_get_str(r, "bk_psk",    bk_psk,    &n);
    n = sizeof(bk_broker); nvs_get_str(r, "bk_broker", bk_broker, &n);
    n = sizeof(bk_ntp);    nvs_get_str(r, "bk_ntp",    bk_ntp,    &n);
    n = sizeof(bk_host);   nvs_get_str(r, "bk_host",   bk_host,   &n);
    nvs_close(r);
    if (ep != ESP_OK || !pending) return;

    if (tries >= RP_MAX_TRIES) {
        // Revert: restore the backed-up config into "ha", clear pending, reboot onto the last-good net.
        nvs_handle_t h;
        if (nvs_open("ha", NVS_READWRITE, &h) == ESP_OK) {
            nvs_set_str(h, "wifi_ssid", bk_ssid);
            nvs_set_str(h, "wifi_psk", bk_psk);
            nvs_set_str(h, "broker_uri", bk_broker);
            nvs_set_str(h, "ntp_server", bk_ntp);
            nvs_set_str(h, "ota_host", bk_host);
            nvs_commit(h);
            nvs_close(h);
        }
        if (nvs_open(RP_NS, NVS_READWRITE, &r) == ESP_OK) {
            nvs_set_u8(r, "pending", 0); nvs_set_u8(r, "tries", 0); nvs_commit(r); nvs_close(r);
        }
        ESP_LOGE(TAG, "repoint REVERTED after %d failed boots -> restoring ssid=%s broker=%s; rebooting",
                 tries, bk_ssid, bk_broker);
        vTaskDelay(pdMS_TO_TICKS(500));
        esp_restart();
    }
    // Count this trial boot and continue (the late confirm clears pending if the broker comes up).
    if (nvs_open(RP_NS, NVS_READWRITE, &r) == ESP_OK) {
        nvs_set_u8(r, "tries", (uint8_t)(tries + 1)); nvs_commit(r); nvs_close(r);
    }
    ESP_LOGW(TAG, "repoint trial boot %d/%d (awaiting broker confirm)", tries + 1, RP_MAX_TRIES);
}

void ha_config_repoint_confirm(bool (*is_healthy)(void *user), void *user, int timeout_ms) {
    nvs_handle_t r;
    if (nvs_open(RP_NS, NVS_READONLY, &r) != ESP_OK) return;
    uint8_t pending = 0;
    esp_err_t ep = nvs_get_u8(r, "pending", &pending);
    nvs_close(r);
    if (ep != ESP_OK || !pending) return;

    int waited = 0;
    while (waited < timeout_ms) {
        if (is_healthy && is_healthy(user)) {
            if (nvs_open(RP_NS, NVS_READWRITE, &r) == ESP_OK) {
                nvs_set_u8(r, "pending", 0); nvs_set_u8(r, "tries", 0); nvs_commit(r); nvs_close(r);
            }
            ESP_LOGI(TAG, "repoint CONFIRMED (broker reachable) — committed");
            return;
        }
        vTaskDelay(pdMS_TO_TICKS(500));
        waited += 500;
    }
    ESP_LOGE(TAG, "repoint NOT confirmed in %dms — rebooting to retry/revert", timeout_ms);
    esp_restart();
}
