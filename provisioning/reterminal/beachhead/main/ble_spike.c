// Spike 0 (ADR-0019 Phase 6): NimBLE observer over esp-hosted VHCI.
// Decisive feasibility test — does the P4's NimBLE host receive adverts through the
// factory C6 slave (esp_hosted host 2.12.9 <-> slave 2.3.0)? Passive scan only; no
// decode/dedup/relay (that's Stage 1). Every advert bumps counters; the first few
// are logged in full so we can eyeball real MACs/RSSI over the MQTT debug firehose.
//
// Discipline (same as the display bring-up): this runs on its own task, never on the
// MQTT-callback stack, and every failure path returns cleanly so a bad BLE bring-up
// can't knock the panel/OTA lifeline off the bus.
#include "ble_spike.h"
#include <string.h>
#include "esp_log.h"
#include "esp_hosted.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/util/util.h"

static const char *TAG = "ble_spike";

static uint8_t  own_addr_type;
static volatile uint32_t s_total_adv;
static volatile int8_t   s_last_rssi;
static volatile bool     s_running;

#define UNIQ_SLOTS 64
static uint8_t  s_uniq[UNIQ_SLOTS][6];
static volatile uint32_t s_uniq_n;   // may exceed UNIQ_SLOTS; storage caps, count doesn't

static void note_uniq(const uint8_t *val) {
    uint32_t have = s_uniq_n < UNIQ_SLOTS ? s_uniq_n : UNIQ_SLOTS;
    for (uint32_t i = 0; i < have; i++)
        if (memcmp(s_uniq[i], val, 6) == 0) return;
    if (s_uniq_n < UNIQ_SLOTS) memcpy(s_uniq[s_uniq_n], val, 6);
    s_uniq_n++;
}

void ble_spike_stats(uint32_t *total, uint32_t *uniq, int8_t *rssi) {
    if (total) *total = s_total_adv;
    if (uniq)  *uniq  = s_uniq_n;
    if (rssi)  *rssi  = s_last_rssi;
}

bool ble_spike_running(void) { return s_running; }

static void start_scan(void);

static int gap_event(struct ble_gap_event *event, void *arg) {
    if (event->type == BLE_GAP_EVENT_DISC_COMPLETE) {
        start_scan();   // BLE_HS_FOREVER shouldn't complete, but re-arm defensively
        return 0;
    }
    if (event->type != BLE_GAP_EVENT_DISC) return 0;

    s_total_adv++;
    s_last_rssi = event->disc.rssi;
    note_uniq(event->disc.addr.val);

    if (s_total_adv <= 12) {
        const uint8_t *a = event->disc.addr.val;   // LE order; print display order
        ESP_LOGW(TAG, "adv #%u %02X:%02X:%02X:%02X:%02X:%02X rssi=%d len=%d type=%d",
                 (unsigned)s_total_adv, a[5], a[4], a[3], a[2], a[1], a[0],
                 event->disc.rssi, event->disc.length_data, event->disc.event_type);
    }
    return 0;
}

static void start_scan(void) {
    struct ble_gap_disc_params dp = {0};
    dp.passive = 1;
    dp.filter_duplicates = 0;
    dp.itvl   = BLE_GAP_SCAN_FAST_INTERVAL_MIN;
    dp.window = BLE_GAP_SCAN_FAST_WINDOW;
    int rc = ble_gap_disc(own_addr_type, BLE_HS_FOREVER, &dp, gap_event, NULL);
    if (rc != 0) {
        s_running = false;
        ESP_LOGE(TAG, "ble_gap_disc failed rc=%d", rc);
    } else {
        s_running = true;
        ESP_LOGW(TAG, "passive observer scan started (own_addr_type=%d)", own_addr_type);
    }
}

static void on_sync(void) {
    int rc = ble_hs_util_ensure_addr(0);
    if (rc != 0) { ESP_LOGE(TAG, "ensure_addr rc=%d", rc); return; }
    rc = ble_hs_id_infer_auto(0, &own_addr_type);
    if (rc != 0) { ESP_LOGE(TAG, "infer_auto rc=%d", rc); return; }
    ESP_LOGW(TAG, "nimble host synced — starting scan");
    start_scan();
}

static void on_reset(int reason) {
    s_running = false;
    ESP_LOGW(TAG, "nimble host reset; reason=%d", reason);
}

static void host_task(void *param) {
    nimble_port_run();               // blocks until nimble_port_stop()
    nimble_port_freertos_deinit();
}

void ble_spike_start(void) {
    static volatile bool started = false;
    if (started) { ESP_LOGW(TAG, "already started"); return; }
    started = true;

    ESP_LOGW(TAG, "Spike 0: init hosted BT controller over VHCI...");
    esp_err_t e = esp_hosted_bt_controller_init();
    if (e != ESP_OK) { ESP_LOGE(TAG, "bt_controller_init failed 0x%x", e); started = false; return; }
    e = esp_hosted_bt_controller_enable();
    if (e != ESP_OK) { ESP_LOGE(TAG, "bt_controller_enable failed 0x%x", e); started = false; return; }

    esp_err_t r = nimble_port_init();
    if (r != ESP_OK) { ESP_LOGE(TAG, "nimble_port_init failed 0x%x", r); started = false; return; }

    ble_hs_cfg.sync_cb  = on_sync;
    ble_hs_cfg.reset_cb = on_reset;
    nimble_port_freertos_init(host_task);
    ESP_LOGW(TAG, "Spike 0: nimble host launched — waiting for sync");
}
