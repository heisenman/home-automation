// OTA update with self-test + auto-rollback — see ha_ota.h.
#include "ha_ota.h"
#include "ha_mqtt.h"
#include "ble_scan.h"
#include <string.h>
#include "esp_log.h"
#include "esp_system.h"
#include "esp_ota_ops.h"
#include "esp_https_ota.h"
#include "esp_http_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ha_ota";
static volatile bool s_busy;

bool ha_ota_busy(void) { return s_busy; }

void ha_ota_confirm_if_pending(void) {
    const esp_partition_t *run = esp_ota_get_running_partition();
    esp_ota_img_states_t st;
    if (esp_ota_get_state_partition(run, &st) != ESP_OK) return;
    if (st != ESP_OTA_IMG_PENDING_VERIFY) {
        ESP_LOGI(TAG, "running %s (state=%d) — not a trial image", run->label, st);
        return;                                  // normal (cable-flashed / already-valid) boot
    }

    // Trial image: self-test before committing. The main remote-brick risk is an update that
    // breaks connectivity — so require the network path (Wi-Fi + broker) to come up.
    ha_mqtt_log("OTA trial image on %s — running self-test (waiting for MQTT)...", run->label);
    bool ok = false;
    for (int i = 0; i < 30; i++) {               // up to ~15 s
        if (ha_mqtt_is_connected()) { ok = true; break; }
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    if (ok) {
        esp_ota_mark_app_valid_cancel_rollback();
        ha_mqtt_log("OTA self-test PASS — image confirmed valid on %s", run->label);
    } else {
        ha_mqtt_log("OTA self-test FAIL (no MQTT) — rolling back to previous slot");
        vTaskDelay(pdMS_TO_TICKS(300));          // let the log flush
        esp_ota_mark_app_invalid_rollback_and_reboot();   // does not return
    }
}

static char s_url[256];

static void ota_task(void *arg) {
    const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
    ha_mqtt_log("OTA start: url=%s -> slot %s", s_url, next ? next->label : "?");

    ha_ble_scan_pause();                         // single radio: don't fight Wi-Fi during the download

    esp_http_client_config_t http = {
        .url = s_url,
        .timeout_ms = 20000,
        .keep_alive_enable = true,
    };
    esp_https_ota_config_t cfg = { .http_config = &http };

    esp_err_t err = esp_https_ota(&cfg);
    if (err == ESP_OK) {
        ha_mqtt_log("OTA write OK — rebooting into %s (pending verify)", next ? next->label : "?");
        vTaskDelay(pdMS_TO_TICKS(800));          // flush MQTT
        esp_restart();                           // boots new slot; self-test confirms or rolls back
    } else {
        ha_mqtt_log("OTA FAILED: %s — staying on current image", esp_err_to_name(err));
        ha_ble_scan_resume();
    }
    s_busy = false;
    vTaskDelete(NULL);
}

bool ha_ota_start(const char *url) {
    if (s_busy) { ESP_LOGW(TAG, "OTA already running"); return false; }
    if (!url || !url[0]) return false;
    snprintf(s_url, sizeof(s_url), "%s", url);
    s_busy = true;
    // generous stack: TLS/http buffers live here
    if (xTaskCreate(ota_task, "ha_ota", 8192, NULL, 5, NULL) != pdPASS) { s_busy = false; return false; }
    return true;
}
