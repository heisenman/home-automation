// Mesh reach census (ADR-0023) — per-MAC RSSI-EWMA table + on-demand snapshot publish. See ha_reach.h.
#include "ha_reach.h"
#include <string.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"

static const char *TAG = "ha_reach";

// 16 slots >> the ~10 real meters a node can hear — bounds the payload and the census work. RSSI is
// smoothed at the source (decision #2) so a single errant sample never reaches the coordinator; alpha
// ~0.3 is responsive at the 1–10 Hz advert rate yet rejects flutter. Stale endpoints (unheard for a
// while) drop out of the report so a relocation is reflected within a window, not fossilised.
#define REACH_SLOTS          16
#define EWMA_ALPHA_NUM        3          // alpha = 0.3 (3/10)
#define EWMA_ALPHA_DEN       10
#define STALE_MS         (15 * 60 * 1000)
#define DEFAULT_FALLBACK_MS (30 * 60 * 1000)   // ≈2× the coordinator's 900 s trigger cadence
#define FALLBACK_TICK_MS  (60 * 1000)

typedef struct {
    bool     used;
    uint8_t  mac[6];        // display order
    float    ewma;          // smoothed RSSI, dBm
    uint16_t count;         // sightings since the last report (per-window)
    int64_t  last_ms;       // esp_log_timestamp() at the last sighting
} reach_slot_t;

static reach_slot_t     s_tbl[REACH_SLOTS];
static portMUX_TYPE     s_mux = portMUX_INITIALIZER_UNLOCKED;
static ha_reach_cfg_t   s_cfg;
static volatile int64_t s_last_report_ms;

void ha_reach_note(const uint8_t mac[6], int rssi) {
    int64_t now = esp_log_timestamp();
    portENTER_CRITICAL(&s_mux);
    reach_slot_t *slot = NULL, *free_slot = NULL, *oldest = &s_tbl[0];
    for (int i = 0; i < REACH_SLOTS; i++) {
        if (s_tbl[i].used && memcmp(s_tbl[i].mac, mac, 6) == 0) { slot = &s_tbl[i]; break; }
        if (!s_tbl[i].used && !free_slot) free_slot = &s_tbl[i];
        if (s_tbl[i].used && s_tbl[i].last_ms < oldest->last_ms) oldest = &s_tbl[i];
    }
    if (!slot) slot = free_slot ? free_slot : oldest;   // reuse a free slot, else LRU-evict the stalest
    if (!slot->used || memcmp(slot->mac, mac, 6) != 0) {
        slot->used = true; memcpy(slot->mac, mac, 6);
        slot->ewma = (float)rssi; slot->count = 0;      // seed EWMA to the first sample
    } else {
        slot->ewma += ((float)rssi - slot->ewma) * EWMA_ALPHA_NUM / EWMA_ALPHA_DEN;
    }
    if (slot->count < 0xFFFF) slot->count++;
    slot->last_ms = now;
    portEXIT_CRITICAL(&s_mux);
}

void ha_reach_report(void) {
    int64_t now = esp_log_timestamp();
    reach_slot_t snap[REACH_SLOTS];
    portENTER_CRITICAL(&s_mux);                         // snapshot under the lock; build JSON outside it
    memcpy(snap, s_tbl, sizeof(snap));
    for (int i = 0; i < REACH_SLOTS; i++) s_tbl[i].count = 0;   // reset the per-window sighting counts
    portEXIT_CRITICAL(&s_mux);
    s_last_report_ms = now;

    char buf[1024];
    int n = 0;
    buf[n++] = '[';
    bool first = true;
    for (int i = 0; i < REACH_SLOTS; i++) {
        if (!snap[i].used || snap[i].count == 0) continue;   // heard nothing this window → omit
        if (now - snap[i].last_ms > STALE_MS) continue;      // gone quiet → let it drop out
        const uint8_t *m = snap[i].mac;
        int age_s = (int)((now - snap[i].last_ms) / 1000);
        int rssi  = (int)(snap[i].ewma >= 0 ? snap[i].ewma + 0.5f : snap[i].ewma - 0.5f);
        int w = snprintf(buf + n, sizeof(buf) - n,
            "%s{\"mac\":\"%02X:%02X:%02X:%02X:%02X:%02X\",\"rssi_ewma\":%d,\"count\":%u,\"age_s\":%d}",
            first ? "" : ",", m[0], m[1], m[2], m[3], m[4], m[5], rssi, (unsigned)snap[i].count, age_s);
        if (w <= 0 || n + w >= (int)sizeof(buf) - 2) break;  // leave room for the closing ']'
        n += w; first = false;
    }
    buf[n++] = ']';
    buf[n] = '\0';
    if (s_cfg.publish) s_cfg.publish(buf);
}

// Low-rate safety net: server-push is the primary cadence, but if no trigger has landed within the
// fallback window (silent/absent coordinator), self-report so this node never goes invisible.
static void fallback_task(void *arg) {
    (void)arg;
    uint32_t fb = s_cfg.fallback_ms ? s_cfg.fallback_ms : DEFAULT_FALLBACK_MS;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(FALLBACK_TICK_MS));
        if (esp_log_timestamp() - s_last_report_ms >= (int64_t)fb) {
            ESP_LOGI(TAG, "no census trigger in %ums — self-reporting reach", (unsigned)fb);
            ha_reach_report();
        }
    }
}

void ha_reach_start(const ha_reach_cfg_t *cfg) {
    if (!cfg) { ESP_LOGE(TAG, "null cfg"); return; }
    s_cfg = *cfg;
    s_last_report_ms = esp_log_timestamp();
    xTaskCreate(fallback_task, "reach", 4096, NULL, 3, NULL);
    ESP_LOGI(TAG, "reach census up (%d slots, fallback %ums)",
             REACH_SLOTS, (unsigned)(s_cfg.fallback_ms ? s_cfg.fallback_ms : DEFAULT_FALLBACK_MS));
}
