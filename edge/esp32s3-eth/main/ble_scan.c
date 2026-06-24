#include "ble_scan.h"
#include "ha_mqtt.h"
#include "ha_relay.h"
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
static volatile bool s_paused;

// The S3/C6 SHARE one 2.4GHz radio between BLE and Wi-Fi. A continuous scan (window==itvl) hogs the radio
// and starves the Wi-Fi beacon (bcn_timeout → drops). Transport-aware duty cycle: scan continuously on
// Ethernet (no contention, max advert capture); on Wi-Fi, yield ~60% of the radio so the link stays up.
// Set once by ha_ble_scan_start(shared_radio). (OTA/GATT still fully pause the scan via ha_ble_scan_pause.)
#define SCAN_ITVL_FULL    BLE_GAP_SCAN_FAST_INTERVAL_MIN   // window==itvl ⇒ scan 100% of the time
#define SCAN_WINDOW_FULL  BLE_GAP_SCAN_FAST_WINDOW
#define SCAN_ITVL_WIFI    160U   // 100 ms (units of 0.625 ms)
#define SCAN_WINDOW_WIFI   64U   // 40 ms  ⇒ ~40% duty, ~60% of the radio left for Wi-Fi
static uint16_t s_scan_itvl = SCAN_ITVL_FULL;
static uint16_t s_scan_window = SCAN_WINDOW_FULL;

uint8_t ha_ble_own_addr_type(void) { return own_addr_type; }

// ── Per-MAC cache (debounce + address-type lookup for GATT connect) ─────────────
#define DEDUP_SLOTS         48
#define REPUBLISH_MIN_MS    30000
#define TEMP_EPS            0.1f
#define HUM_EPS             1
#define BATT_EPS            1
typedef struct {
    uint8_t mac[6];           // display order (reversed from addr.val)
    ble_addr_t addr;          // full BLE address (type + LE val) as seen on air
    bool used;
    int64_t last_ms;
    float t; int h; int b;
} dedup_t;
static dedup_t s_seen[DEDUP_SLOTS];

static dedup_t *find_or_alloc(const uint8_t mac[6]) {
    dedup_t *free_slot = NULL;
    for (int i = 0; i < DEDUP_SLOTS; i++) {
        if (s_seen[i].used && memcmp(s_seen[i].mac, mac, 6) == 0) return &s_seen[i];
        if (!s_seen[i].used && !free_slot) free_slot = &s_seen[i];
    }
    if (!free_slot) free_slot = &s_seen[0];
    return free_slot;
}

static bool should_publish(dedup_t *slot, const uint8_t mac[6], const sb_reading_t *r) {
    int64_t now = esp_log_timestamp();
    if (!slot->used || memcmp(slot->mac, mac, 6) != 0) {
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

bool ha_ble_lookup_addr(const char *mac_str, ble_addr_t *out) {
    uint8_t mac[6];
    if (sscanf(mac_str, "%2hhx:%2hhx:%2hhx:%2hhx:%2hhx:%2hhx",
               &mac[0], &mac[1], &mac[2], &mac[3], &mac[4], &mac[5]) != 6) return false;
    for (int i = 0; i < DEDUP_SLOTS; i++) {
        if (s_seen[i].used && memcmp(s_seen[i].mac, mac, 6) == 0) { *out = s_seen[i].addr; return true; }
    }
    return false;
}

// ── Passive-scan adv handler ────────────────────────────────────────────────────
static int gap_event(struct ble_gap_event *event, void *arg);

static void start_scan(void) {
    struct ble_gap_disc_params dp = {0};
    dp.passive = 1; dp.filter_duplicates = 0;
    dp.itvl = s_scan_itvl; dp.window = s_scan_window;
    int rc = ble_gap_disc(own_addr_type, BLE_HS_FOREVER, &dp, gap_event, NULL);
    if (rc != 0) ESP_LOGE(TAG, "ble_gap_disc failed rc=%d", rc);
    else ESP_LOGI(TAG, "passive scan started");
}

void ha_ble_scan_pause(void)  { s_paused = true;  ble_gap_disc_cancel(); }
void ha_ble_scan_resume(void) { s_paused = false; start_scan(); }

static int gap_event(struct ble_gap_event *event, void *arg) {
    if (event->type == BLE_GAP_EVENT_DISC_COMPLETE) {
        if (!s_paused) start_scan();   // don't restart while a GATT pull holds the radio
        return 0;
    }
    if (event->type != BLE_GAP_EVENT_DISC) return 0;

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
        if (type == 0xFF && vlen >= 2) {
            uint16_t company = val[0] | (val[1] << 8);
            if (company == SB_MFR_COMPANY_ID) { mfr = val + 2; mfr_len = vlen - 2; has_0969 = true; }
        } else if (type == 0x16 && vlen >= 2) {
            uint16_t uuid = val[0] | (val[1] << 8);
            if (uuid == SB_SVC_UUID16) { svc = val + 2; svc_len = vlen - 2; has_fd3d = true; }
        }
        i += 1 + flen;
    }
    if (!sb_is_switchbot(has_0969, has_fd3d)) return 0;

    sb_reading_t r;
    if (!sb_decode(svc, svc_len, mfr, mfr_len, &r) || !r.valid) return 0;

    const uint8_t *a = event->disc.addr.val;
    uint8_t mac[6] = { a[5], a[4], a[3], a[2], a[1], a[0] };
    dedup_t *slot = find_or_alloc(mac);
    slot->addr = event->disc.addr;     // cache full address (type + val) for GATT connect
    if (!should_publish(slot, mac, &r)) return 0;

    char mac_str[18];
    snprintf(mac_str, sizeof(mac_str), "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    if (!ha_relay_allowed(mac_str)) return 0;   // Phase B: only relay meters the dictator assigned us
    ha_mqtt_publish_reading(mac_str, &r, event->disc.rssi);
    return 0;
}

static void on_sync(void) {
    ble_hs_util_ensure_addr(0);
    ble_hs_id_infer_auto(0, &own_addr_type);
    start_scan();
}
static void on_reset(int reason) { ESP_LOGW(TAG, "nimble reset; reason=%d", reason); }
static void host_task(void *param) { nimble_port_run(); nimble_port_freertos_deinit(); }

void ha_ble_scan_start(bool shared_radio) {
    if (shared_radio) {                 // Wi-Fi shares the radio → duty-cycle so the link survives
        s_scan_itvl = SCAN_ITVL_WIFI;
        s_scan_window = SCAN_WINDOW_WIFI;
    }
    ESP_LOGI(TAG, "BLE scan: %s (itvl=%u window=%u ×0.625ms)",
             shared_radio ? "duty-cycled for Wi-Fi coexistence" : "continuous (wired)",
             s_scan_itvl, s_scan_window);
    ESP_ERROR_CHECK(nimble_port_init());
    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.reset_cb = on_reset;
    nimble_port_freertos_init(host_task);
}
