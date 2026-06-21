#include "ble_scan.h"
#include "ha_mqtt.h"
#include "switchbot_decode.h"
#include <string.h>
#include <stdio.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"

static const char *TAG = "ble_scan";
static uint8_t own_addr_type;

// ── Per-MAC debounce (skip noise; bound publish rate) ──────────────────────────
#define DEDUP_SLOTS         48
#define REPUBLISH_MIN_MS    30000   // always allow a refresh after this long
#define TEMP_EPS            0.1f
#define HUM_EPS             1
#define BATT_EPS            1
typedef struct {
    uint8_t mac[6];
    bool used;
    int64_t last_ms;
    float t; int h; int b;
} dedup_t;
static dedup_t s_seen[DEDUP_SLOTS];

static bool should_publish(const uint8_t mac[6], const sb_reading_t *r) {
    int64_t now = esp_log_timestamp();   // ms since boot
    dedup_t *slot = NULL, *free_slot = NULL;
    for (int i = 0; i < DEDUP_SLOTS; i++) {
        if (s_seen[i].used && memcmp(s_seen[i].mac, mac, 6) == 0) { slot = &s_seen[i]; break; }
        if (!s_seen[i].used && !free_slot) free_slot = &s_seen[i];
    }
    if (!slot) {                          // first time seeing this MAC
        slot = free_slot ? free_slot : &s_seen[0];
        memcpy(slot->mac, mac, 6); slot->used = true;
        slot->last_ms = now; slot->t = r->temperature_c; slot->h = r->humidity_pct; slot->b = r->battery_pct;
        return true;
    }
    bool changed = (r->temperature_c - slot->t > TEMP_EPS) || (slot->t - r->temperature_c > TEMP_EPS)
                || (r->humidity_pct - slot->h >= HUM_EPS) || (slot->h - r->humidity_pct >= HUM_EPS)
                || (r->battery_pct >= 0 && (r->battery_pct - slot->b >= BATT_EPS || slot->b - r->battery_pct >= BATT_EPS));
    if (changed || (now - slot->last_ms) >= REPUBLISH_MIN_MS) {
        slot->last_ms = now; slot->t = r->temperature_c; slot->h = r->humidity_pct; slot->b = r->battery_pct;
        return true;
    }
    return false;
}

// ── AD parsing ─────────────────────────────────────────────────────────────────
static int gap_event(struct ble_gap_event *event, void *arg) {
    if (event->type != BLE_GAP_EVENT_DISC) {
        if (event->type == BLE_GAP_EVENT_DISC_COMPLETE) {
            ESP_LOGW(TAG, "scan ended (%d) — restarting", event->disc_complete.reason);
            struct ble_gap_disc_params dp = {0};
            dp.passive = 1; dp.filter_duplicates = 0;
            dp.itvl = BLE_GAP_SCAN_FAST_INTERVAL_MIN; dp.window = BLE_GAP_SCAN_FAST_WINDOW;
            ble_gap_disc(own_addr_type, BLE_HS_FOREVER, &dp, gap_event, NULL);
        }
        return 0;
    }

    const uint8_t *d = event->disc.data;
    int len = event->disc.length_data;
    const uint8_t *svc = NULL, *mfr = NULL;
    int svc_len = 0, mfr_len = 0;
    bool has_0969 = false, has_fd3d = false;

    for (int i = 0; i + 1 < len; ) {
        uint8_t flen = d[i];
        if (flen == 0 || i + 1 + flen > len) break;
        uint8_t type = d[i + 1];
        const uint8_t *val = &d[i + 2];
        int vlen = flen - 1;
        if (type == 0xFF && vlen >= 2) {                        // manufacturer specific
            uint16_t company = val[0] | (val[1] << 8);
            if (company == SB_MFR_COMPANY_ID) { mfr = val + 2; mfr_len = vlen - 2; has_0969 = true; }
        } else if (type == 0x16 && vlen >= 2) {                 // service data, 16-bit UUID
            uint16_t uuid = val[0] | (val[1] << 8);
            if (uuid == SB_SVC_UUID16) { svc = val + 2; svc_len = vlen - 2; has_fd3d = true; }
        }
        i += 1 + flen;
    }

    if (!sb_is_switchbot(has_0969, has_fd3d)) return 0;

    sb_reading_t r;
    if (!sb_decode(svc, svc_len, mfr, mfr_len, &r) || !r.valid) return 0;

    // addr.val is little-endian; display/registry MAC is reversed
    const uint8_t *a = event->disc.addr.val;
    uint8_t mac[6] = { a[5], a[4], a[3], a[2], a[1], a[0] };
    if (!should_publish(mac, &r)) return 0;

    char mac_str[18];
    snprintf(mac_str, sizeof(mac_str), "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    ha_mqtt_publish_reading(mac_str, &r, event->disc.rssi);
    return 0;
}

static void start_scan(void) {
    struct ble_gap_disc_params dp = {0};
    dp.passive = 1;            // passive scan — don't send scan requests (don't disturb devices)
    dp.filter_duplicates = 0;  // we want every advert; debounce ourselves
    dp.itvl = BLE_GAP_SCAN_FAST_INTERVAL_MIN;
    dp.window = BLE_GAP_SCAN_FAST_WINDOW;
    int rc = ble_gap_disc(own_addr_type, BLE_HS_FOREVER, &dp, gap_event, NULL);
    if (rc != 0) ESP_LOGE(TAG, "ble_gap_disc failed rc=%d", rc);
    else ESP_LOGI(TAG, "passive scan started");
}

static void on_sync(void) {
    ble_hs_util_ensure_addr(0);
    ble_hs_id_infer_auto(0, &own_addr_type);
    start_scan();
}
static void on_reset(int reason) { ESP_LOGW(TAG, "nimble reset; reason=%d", reason); }

static void host_task(void *param) {
    nimble_port_run();          // returns only on nimble_port_stop()
    nimble_port_freertos_deinit();
}

void ha_ble_scan_start(void) {
    ESP_ERROR_CHECK(nimble_port_init());
    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.reset_cb = on_reset;
    nimble_port_freertos_init(host_task);
}
