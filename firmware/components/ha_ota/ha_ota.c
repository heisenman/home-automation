// Shared OTA client (ADR-0020) — host pin + identity gate + signed-hash + self-test/rollback. Ported from
// edge/esp32c6/main/ha_ota.c; platform deps (log / health / radio / LED) are cfg seams (ha_ota_init) so it
// links on every board and the panel with no ha_mqtt / ble_scan / ha_led dependency.
#include "ha_ota.h"
#include <string.h>
#include <stdarg.h>
#include "esp_log.h"
#include "esp_system.h"
#include "esp_ota_ops.h"
#include "esp_https_ota.h"
#include "esp_app_desc.h"       // esp_app_desc_t — the incoming image's identity (OTA node-id gate)
#include "esp_http_client.h"
#include "esp_partition.h"
#include "mbedtls/sha256.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ha_ota";
static volatile bool s_busy;

// ── Platform seams (ha_ota_init) ─────────────────────────────────────────────────
static ha_ota_cfg_t s_cfg;
static char s_node_id[32];               // OWN copy of node_id — never hold the caller's pointer
void ha_ota_init(const ha_ota_cfg_t *cfg) {
    if (!cfg) return;
    s_cfg = *cfg;
    // The identity gate dereferences node_id LATER (at OTA time), long after ha_ota_init returned. Copy it
    // into our own storage instead of trusting the caller's pointer to still be alive — a dangling node_id
    // reads as "unknown" and rejects EVERY OTA (2026-07-08: app_main passed a pointer into its stack cfg).
    if (cfg->node_id) { snprintf(s_node_id, sizeof(s_node_id), "%s", cfg->node_id); s_cfg.node_id = s_node_id; }
    else s_cfg.node_id = NULL;
}

static const char *node_id(void) { return (s_cfg.node_id && s_cfg.node_id[0]) ? s_cfg.node_id : "unknown"; }

// Diagnostic log -> cfg.log (was ha_mqtt_log on the edge). Always mirrored to ESP_LOG.
static void ota_log(const char *fmt, ...) {
    char b[160];
    va_list ap; va_start(ap, fmt); vsnprintf(b, sizeof(b), fmt, ap); va_end(ap);
    ESP_LOGI(TAG, "%s", b);
    if (s_cfg.log) s_cfg.log(b, s_cfg.user);
}
static void radio_pause(void)  { if (s_cfg.radio_pause)  s_cfg.radio_pause(s_cfg.user); }
static void radio_resume(void) { if (s_cfg.radio_resume) s_cfg.radio_resume(s_cfg.user); }
static void on_fail(void)      { if (s_cfg.on_fail)      s_cfg.on_fail(s_cfg.user); }

// Compare the host in "scheme://host[:port][/path]" against the pinned cfg.ota_host.
static bool ota_host_pinned_ok(const char *url) {
    const char *host = s_cfg.ota_host;
    if (!host || !host[0]) { ota_log("OTA host pin DISABLED (no ota_host)"); return true; }
    const char *p = strstr(url, "://");
    p = p ? p + 3 : url;
    size_t n = strcspn(p, ":/");                  // host = up to ':' (port) or '/' (path)
    bool ok = (n == strlen(host)) && (strncmp(p, host, n) == 0);
    if (!ok) ota_log("OTA REJECTED: host not pinned (allowed: %s)", host);
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

    // Trial image: self-test before committing. The main remote-brick risk is an update that breaks
    // connectivity — so require the network path (cfg.is_healthy, e.g. broker) to come up.
    ota_log("OTA trial image on %s — running self-test...", run->label);
    bool ok = (s_cfg.is_healthy == NULL);        // no health seam → nothing to test, accept
    for (int i = 0; !ok && i < 30; i++) {        // up to ~15 s
        if (s_cfg.is_healthy(s_cfg.user)) { ok = true; break; }
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    if (ok) {
        esp_ota_mark_app_valid_cancel_rollback();
        ota_log("OTA self-test PASS — image confirmed valid on %s", run->label);
    } else {
        ota_log("OTA self-test FAIL — rolling back to previous slot");
        vTaskDelay(pdMS_TO_TICKS(300));          // let the log flush
        esp_ota_mark_app_invalid_rollback_and_reboot();   // does not return
    }
}

static char s_url[256];
static char s_sha256[65];        // expected image hash (hex), from the SIGNED directive ("" = skip)

// Read the just-written update partition back and compare its SHA-256 to the signed expected value.
// This turns rollback (anti-brick) into authenticity (anti-malice): only an image whose bytes match the
// directive's signed hash is allowed to boot. img_len = bytes esp_https_ota actually wrote.
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
    ota_log("OTA start: url=%s -> slot %s (hash %s)", s_url, next ? next->label : "?",
            s_sha256[0] ? "required" : "none");
    radio_pause();                                // single radio: don't fight Wi-Fi during the download

    esp_http_client_config_t http = { .url = s_url, .timeout_ms = 20000, .keep_alive_enable = true };
    esp_https_ota_config_t cfg = { .http_config = &http };
    esp_https_ota_handle_t h = NULL;
    esp_err_t err = esp_https_ota_begin(&cfg, &h);
    if (err != ESP_OK || !h) { ota_log("OTA begin failed: %s", esp_err_to_name(err)); goto fail_noh; }

    // Identity gate (ADR-0020): the image's app version is built as "<node_id>@<ver>" (PROJECT_VER via
    // version.txt). Refuse an image branded for a DIFFERENT node BEFORE writing/booting it, so a
    // cross-provisioned push (cf. 2026-07-05 cbed_c6<-coffice_c6) can never come up wearing this node's
    // slot. The descriptor is in the header read by esp_https_ota_begin.
    esp_app_desc_t img_desc;
    if (esp_https_ota_get_img_desc(h, &img_desc) != ESP_OK) {
        ota_log("OTA REJECTED: no image descriptor"); goto fail;
    }
    {
        char want[48]; snprintf(want, sizeof(want), "%s@", node_id());
        if (strncmp(img_desc.version, want, strlen(want)) != 0) {
            ota_log("OTA REJECTED: image built for another node (version='%s', this node=%s)",
                    img_desc.version, node_id());
            goto fail;
        }
    }
    ota_log("OTA identity OK: image is for %s (version=%s)", node_id(), img_desc.version);

    do { err = esp_https_ota_perform(h); } while (err == ESP_ERR_HTTPS_OTA_IN_PROGRESS);
    if (err != ESP_OK || !esp_https_ota_is_complete_data_received(h)) {
        ota_log("OTA download failed: %s", esp_err_to_name(err)); goto fail;
    }
    if (!image_hash_ok(h)) {                       // authenticity gate — abort BEFORE setting boot
        ota_log("OTA REJECTED: image hash != signed value — not flashing"); goto fail;
    }
    if (s_sha256[0]) ota_log("OTA image hash verified");
    err = esp_https_ota_finish(h);                 // validates image + sets boot partition
    if (err != ESP_OK) { ota_log("OTA finish failed: %s", esp_err_to_name(err)); h = NULL; goto fail; }

    ota_log("OTA write OK — rebooting into %s (pending verify)", next ? next->label : "?");
    vTaskDelay(pdMS_TO_TICKS(800));
    esp_restart();                                 // boots new slot; self-test confirms or rolls back

fail:
    if (h) esp_https_ota_abort(h);
fail_noh:
    on_fail();                                     // e.g. LED feedback; node keeps relaying the old image
    radio_resume();
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
