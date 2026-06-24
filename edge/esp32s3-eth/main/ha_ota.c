// OTA update with self-test + auto-rollback — see ha_ota.h.
#include "ha_ota.h"
#include "ha_mqtt.h"
#include "ha_led.h"
#include "ble_scan.h"
#include <string.h>
#include "esp_log.h"
#include "esp_system.h"
#include "esp_ota_ops.h"
#include "esp_https_ota.h"
#include "esp_http_client.h"
#include "esp_partition.h"
#include "mbedtls/sha256.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#if __has_include("secrets.h")
#include "secrets.h"
#endif

// Node-side lockdown: pin OTA downloads to the dictator's host so a (validly-signed) directive cannot
// point the node at an arbitrary image server. Default = the dictator; override in secrets.h for a dev
// box that serves images itself, or set "" to disable the pin (logged). The image-hash gate still
// applies on top; this just bounds WHERE images may come from.
#ifndef HA_OTA_HOST
#define HA_OTA_HOST "192.168.0.245"
#endif

static const char *TAG = "ha_ota";
static volatile bool s_busy;

// Compare the host in "scheme://host[:port][/path]" against the pinned HA_OTA_HOST.
static bool ota_host_pinned_ok(const char *url) {
    if (!HA_OTA_HOST[0]) { ha_mqtt_log("OTA host pin DISABLED (HA_OTA_HOST empty)"); return true; }
    const char *p = strstr(url, "://");
    p = p ? p + 3 : url;
    size_t n = strcspn(p, ":/");                  // host = up to ':' (port) or '/' (path)
    bool ok = (n == strlen(HA_OTA_HOST)) && (strncmp(p, HA_OTA_HOST, n) == 0);
    if (!ok) ha_mqtt_log("OTA REJECTED: host not pinned (allowed: %s)", HA_OTA_HOST);
    return ok;
}

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
static char s_sha256[65];        // expected image hash (hex), from the SIGNED directive ("" = skip)

// Read the just-written update partition back and compare its SHA-256 to the signed expected value.
// This is what turns rollback (anti-brick) into authenticity (anti-malice): only an image whose bytes
// match the directive's signed hash is allowed to boot. img_len = bytes esp_https_ota actually wrote.
static bool image_hash_ok(esp_https_ota_handle_t h) {
    if (!s_sha256[0]) return true;               // no expected hash supplied → skip (legacy)
    const esp_partition_t *part = esp_ota_get_next_update_partition(NULL);
    int img_len = esp_https_ota_get_image_len_read(h);
    if (!part || img_len <= 0) return false;
    mbedtls_sha256_context sc; mbedtls_sha256_init(&sc); mbedtls_sha256_starts(&sc, 0);
    uint8_t buf[1024];
    for (int off = 0; off < img_len; ) {
        int n = img_len - off; if (n > (int)sizeof(buf)) n = sizeof(buf);
        if (esp_partition_read(part, off, buf, n) != ESP_OK) { mbedtls_sha256_free(&sc); return false; }
        mbedtls_sha256_update(&sc, buf, n); off += n;
    }
    uint8_t dig[32]; mbedtls_sha256_finish(&sc, dig); mbedtls_sha256_free(&sc);
    char hex[65]; for (int i = 0; i < 32; i++) snprintf(hex + i * 2, 3, "%02x", dig[i]);
    return strcmp(hex, s_sha256) == 0;
}

static void ota_task(void *arg) {
    const esp_partition_t *next = esp_ota_get_next_update_partition(NULL);
    ha_mqtt_log("OTA start: url=%s -> slot %s (hash %s)", s_url, next ? next->label : "?",
                s_sha256[0] ? "required" : "none");
    ha_ble_scan_pause();                          // single radio: don't fight Wi-Fi during the download

    esp_http_client_config_t http = { .url = s_url, .timeout_ms = 20000, .keep_alive_enable = true };
    esp_https_ota_config_t cfg = { .http_config = &http };
    esp_https_ota_handle_t h = NULL;
    esp_err_t err = esp_https_ota_begin(&cfg, &h);
    if (err != ESP_OK || !h) { ha_mqtt_log("OTA begin failed: %s", esp_err_to_name(err)); goto fail_noh; }

    do { err = esp_https_ota_perform(h); } while (err == ESP_ERR_HTTPS_OTA_IN_PROGRESS);
    if (err != ESP_OK || !esp_https_ota_is_complete_data_received(h)) {
        ha_mqtt_log("OTA download failed: %s", esp_err_to_name(err)); goto fail;
    }
    if (!image_hash_ok(h)) {                       // authenticity gate — abort BEFORE setting boot
        ha_mqtt_log("OTA REJECTED: image hash != signed value — not flashing"); goto fail;
    }
    if (s_sha256[0]) ha_mqtt_log("OTA image hash verified");
    err = esp_https_ota_finish(h);                 // validates image + sets boot partition
    if (err != ESP_OK) { ha_mqtt_log("OTA finish failed: %s", esp_err_to_name(err)); h = NULL; goto fail; }

    ha_mqtt_log("OTA write OK — rebooting into %s (pending verify)", next ? next->label : "?");
    vTaskDelay(pdMS_TO_TICKS(800));
    esp_restart();                                 // boots new slot; self-test confirms or rolls back

fail:
    if (h) esp_https_ota_abort(h);
fail_noh:
    ha_led_set(HA_LED_OTA_FAIL);                   // MAGENTA x5 — last OTA failed/rejected; sticky until a
                                                   // reboot or an MQTT reconnect (the node keeps relaying the old image)
    ha_ble_scan_resume();
    s_busy = false;
    vTaskDelete(NULL);
}

bool ha_ota_start(const char *url, const char *expected_sha256) {
    if (s_busy) { ESP_LOGW(TAG, "OTA already running"); return false; }
    if (!url || !url[0]) return false;
    if (!ota_host_pinned_ok(url)) return false;   // node-side lockdown: only the pinned host may serve images
    snprintf(s_url, sizeof(s_url), "%s", url);
    snprintf(s_sha256, sizeof(s_sha256), "%s", expected_sha256 ? expected_sha256 : "");
    s_busy = true;
    if (xTaskCreate(ota_task, "ha_ota", 8192, NULL, 5, NULL) != pdPASS) { s_busy = false; return false; }
    return true;
}
