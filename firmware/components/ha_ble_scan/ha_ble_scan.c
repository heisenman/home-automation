// Shared passive BLE observer (ADR-0020) — unified from edge/*/main/ble_scan.c (the
// byte-identical c3/c6 core) with the D1001 panel's VHCI controller bring-up folded in
// behind the controller_init hook. Parse + decode + dedup are verbatim from the edge
// observer so edge- and panel-scanned readings stay identical.
#include "ha_ble_scan.h"
#include <string.h>
#include <stdio.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"

static const char *TAG = "ha_ble_scan";

static ha_ble_scan_cfg_t s_cfg;
static uint8_t  own_addr_type;
static volatile bool s_paused;
static volatile bool s_running;

// observability
static volatile uint32_t s_total_adv;
static volatile uint32_t s_decoded;
static volatile int8_t   s_last_rssi;

uint8_t ha_ble_own_addr_type(void) { return own_addr_type; }
bool    ha_ble_scan_running(void)  { return s_running; }

void ha_ble_scan_stats(uint32_t *total, uint32_t *decoded, int8_t *rssi) {
    if (total)   *total   = s_total_adv;
    if (decoded) *decoded = s_decoded;
    if (rssi)    *rssi    = s_last_rssi;
}

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
    dp.itvl = BLE_GAP_SCAN_FAST_INTERVAL_MIN; dp.window = BLE_GAP_SCAN_FAST_WINDOW;
    int rc = ble_gap_disc(own_addr_type, BLE_HS_FOREVER, &dp, gap_event, NULL);
    if (rc != 0) { s_running = false; ESP_LOGE(TAG, "ble_gap_disc failed rc=%d", rc); }
    else         { s_running = true;  ESP_LOGI(TAG, "passive scan started (own_addr_type=%d)", own_addr_type); }
}

void ha_ble_scan_pause(void)  { s_paused = true;  ble_gap_disc_cancel(); }
void ha_ble_scan_resume(void) { s_paused = false; start_scan(); }

static int gap_event(struct ble_gap_event *event, void *arg) {
    if (event->type == BLE_GAP_EVENT_DISC_COMPLETE) {
        if (!s_paused) start_scan();   // don't restart while a GATT pull holds the radio
        return 0;
    }
    if (event->type != BLE_GAP_EVENT_DISC) return 0;

    s_total_adv++;
    s_last_rssi = event->disc.rssi;

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
    s_decoded++;
    if (s_cfg.on_reading) s_cfg.on_reading(mac_str, &r, event->disc.rssi, s_cfg.user);
    return 0;
}

static void on_sync(void) {
    int rc = ble_hs_util_ensure_addr(0);
    if (rc != 0) { ESP_LOGE(TAG, "ensure_addr rc=%d", rc); return; }
    rc = ble_hs_id_infer_auto(0, &own_addr_type);
    if (rc != 0) { ESP_LOGE(TAG, "infer_auto rc=%d", rc); return; }
    ESP_LOGI(TAG, "nimble host synced — starting scan");
    start_scan();
}
static void on_reset(int reason) { s_running = false; ESP_LOGW(TAG, "nimble reset; reason=%d", reason); }
static void host_task(void *param) { nimble_port_run(); nimble_port_freertos_deinit(); }

void ha_ble_scan_start(const ha_ble_scan_cfg_t *cfg) {
    static volatile bool started = false;
    if (started) { ESP_LOGW(TAG, "already started"); return; }
    if (!cfg)    { ESP_LOGE(TAG, "null cfg"); return; }
    s_cfg = *cfg;

    if (s_cfg.controller_init) {          // platform: bring up the controller (VHCI on the panel)
        esp_err_t e = s_cfg.controller_init();
        if (e != ESP_OK) { ESP_LOGE(TAG, "controller_init failed 0x%x", e); return; }
    }
    esp_err_t r = nimble_port_init();     // native controller is brought up here (edge)
    if (r != ESP_OK) { ESP_LOGE(TAG, "nimble_port_init failed 0x%x", r); return; }

    started = true;
    ble_hs_cfg.sync_cb  = on_sync;
    ble_hs_cfg.reset_cb = on_reset;
    nimble_port_freertos_init(host_task);
    ESP_LOGI(TAG, "nimble host launched — waiting for sync");
}
