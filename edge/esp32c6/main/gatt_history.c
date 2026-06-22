// GATT central client for SwitchBot on-device history. Connects to a meter, runs the
// reverse-engineered handshake/paging (see tools/switchbot_history.py + docs), and relays the
// RAW notifications to home/edge/<node>/<mac>/history for the server to decode. Single radio:
// the passive scan is paused for the pull and resumed on disconnect.
#include "gatt_history.h"
#include "ble_scan.h"
#include "ha_mqtt.h"
#include <string.h>
#include <stdio.h>
#include <time.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "host/ble_hs.h"
#include "host/ble_gap.h"
#include "host/ble_gatt.h"

static const char *TAG = "gatt_hist";

// SwitchBot custom GATT UUIDs (128-bit, NimBLE little-endian byte order)
static const ble_uuid128_t SVC_UUID =
    BLE_UUID128_INIT(0x1b,0xc5,0xd5,0xa5,0x02,0x00,0xb8,0x9f,0xe6,0x11,0x4d,0x22,0x00,0x0d,0xa2,0xcb);
static const ble_uuid128_t CMD_UUID =
    BLE_UUID128_INIT(0x1b,0xc5,0xd5,0xa5,0x02,0x00,0xb8,0x9f,0xe6,0x11,0x4d,0x22,0x02,0x00,0xa2,0xcb);
static const ble_uuid128_t NOTIFY_UUID =
    BLE_UUID128_INIT(0x1b,0xc5,0xd5,0xa5,0x02,0x00,0xb8,0x9f,0xe6,0x11,0x4d,0x22,0x03,0x00,0xa2,0xcb);

// Handshake: 57 00 05 03 04 00 00 00 00 + now(BE u32)
static const uint8_t HANDSHAKE_PREFIX[] = {0x57,0x00,0x05,0x03,0x04,0x00,0x00,0x00,0x00};
#define REC_STRIDE 6
#define HIST_INTERVAL_S 60   // outdoor: ~60s per address-unit (per-minute log), measured from two
                             // metadata captures (Δaddr 56 / Δts 3369s). Used to synthesize oldest_ts so
                             // the server's assign_timestamps gets a consistent interval; reanchor_to_now
                             // then corrects the meter's own clock drift.

typedef struct { const uint8_t *bytes; uint8_t len; } cmd_t;
// meter_pro
static const uint8_t mp_s0[]={0x57,0x0f,0x68,0x05,0x04,0x01,0x03,0x08,0x02,0x00,0x0b,0x01,0x02,0x00,0x0e,0x10};
static const uint8_t mp_s1[]={0x57,0x0f,0x69,0x08,0x01};
static const uint8_t mp_s2[]={0x57,0x0f,0x69,0x08,0x02,0x02};
static const uint8_t mp_s3[]={0x57,0x0f,0x69,0x08,0x02,0x01};
static const uint8_t mp_rp[]={0x57,0x0f,0x69,0x08,0x03,0x02,0x00,0x00};   // + addr(BE)+0x06
// outdoor
static const uint8_t od_s0[]={0x57,0x0f,0x3a};
static const uint8_t od_s1[]={0x57,0x0f,0x3b,0x01};
static const uint8_t od_s2[]={0x57,0x0f,0x3b,0x00};
static const uint8_t od_rp[]={0x57,0x0f,0x3c,0x01,0x00,0x00};             // + addr(BE)+0x06

typedef struct {
    cmd_t setup[4]; int n_setup;
    const uint8_t *read_prefix; int rp_len;
} profile_t;
static const profile_t PROF_METER_PRO = {
    {{mp_s0,sizeof mp_s0},{mp_s1,sizeof mp_s1},{mp_s2,sizeof mp_s2},{mp_s3,sizeof mp_s3}}, 4, mp_rp, sizeof mp_rp };
static const profile_t PROF_OUTDOOR = {
    {{od_s0,sizeof od_s0},{od_s1,sizeof od_s1},{od_s2,sizeof od_s2},{NULL,0}}, 3, od_rp, sizeof od_rp };

// ── State ───────────────────────────────────────────────────────────────────────
#define BIT_DISCOVERED  BIT0
#define BIT_FAIL        BIT1
#define BIT_DISCONNECT  BIT2
#define BIT_CONNECTED   BIT3
#define BATCH_N 20

static volatile bool s_busy;
static EventGroupHandle_t s_evt;
static SemaphoreHandle_t  s_write_sem;
static volatile int s_write_status;

static struct {
    uint16_t conn;
    uint16_t svc_start, svc_end;
    uint16_t cmd_handle, notify_handle;
    char mac_str[18];
    ble_addr_t addr;
    const profile_t *prof;
    // metadata (parsed from notifications)
    volatile bool meta_seen;
    volatile int meta_count;
    volatile bool diag;            // log every notification during the metadata phase
    volatile uint32_t newest_ts, oldest_ts;
    volatile uint16_t newest_ptr, oldest_ptr;
    // outdoor multi-bank history (ADR-0009): read = 570f3c<bank>00<addr:3 BE>06; bank discovered by
    // probing 570f3b00..03 and taking the one whose 0x69 metadata carries a non-zero newest pointer.
    volatile uint8_t  best_bank;
    volatile uint32_t newest_addr;     // full (3-byte) newest pointer of the chosen bank
    volatile uint32_t start_addr;      // address of sample[0] (paging start) — for timestamp mapping
    volatile uint32_t probe_ptr;       // newest pointer parsed during the current bank probe
    volatile bool     probe_got;       // a 0x69 metadata arrived since the probe flag was cleared
    uint32_t pull_now;
    // relay batch (guarded by s_batch_mux)
    char batch[1024];
    int batch_count, total_count, seq;
} g;
static SemaphoreHandle_t s_batch_mutex;

bool gatt_history_busy(void) { return s_busy; }

// ── Relay ───────────────────────────────────────────────────────────────────────
// Serialise the current batch into out[] and reset it. Caller MUST hold s_batch_mutex,
// and MUST publish out[] only AFTER releasing the lock (MQTT publish allocates/locks).
static bool take_batch(char *out, int out_sz) {
    if (g.batch_count == 0) return false;
    int n = snprintf(out, out_sz, "{\"t\":\"data\",\"mac\":\"%s\",\"seq\":%d,\"notifs\":[%s]}",
                     g.mac_str, g.seq++, g.batch);
    g.batch[0] = '\0'; g.batch_count = 0;
    return n > 0 && n < out_sz;
}

static void relay_record(const uint8_t *data, int len) {
    char hex[2 * 20 + 1]; int j = 0;
    for (int i = 0; i < len && i < 20; i++) j += snprintf(hex + j, sizeof(hex) - j, "%02x", data[i]);
    char payload[1024]; bool send = false;
    xSemaphoreTake(s_batch_mutex, portMAX_DELAY);
    if (g.batch_count) strlcat(g.batch, ",", sizeof(g.batch));
    strlcat(g.batch, "\"", sizeof(g.batch));
    strlcat(g.batch, hex, sizeof(g.batch));
    strlcat(g.batch, "\"", sizeof(g.batch));
    g.batch_count++; g.total_count++;
    if (g.batch_count >= BATCH_N) send = take_batch(payload, sizeof(payload));
    xSemaphoreGive(s_batch_mutex);
    if (send) ha_mqtt_publish_history(g.mac_str, payload);   // outside the lock
}

static void flush_remaining(void) {
    char payload[1024]; bool send;
    xSemaphoreTake(s_batch_mutex, portMAX_DELAY);
    send = take_batch(payload, sizeof(payload));
    xSemaphoreGive(s_batch_mutex);
    if (send) ha_mqtt_publish_history(g.mac_str, payload);
}

static void parse_meta(const uint8_t *d, int len) {
    if (len != 15 || d[1] != 0x69) return;
    uint32_t ts = ((uint32_t)d[5]<<24)|((uint32_t)d[6]<<16)|((uint32_t)d[7]<<8)|d[8];
    g.meta_count++;
    if (g.prof == &PROF_OUTDOOR) {
        // newest pointer is the 4-byte BE field at d[9:13] (e.g. 00 01 60 d4 -> 0x000160d4); the read
        // address IS this value. (The old code read only d[11:13], truncating the high bytes -> wrong
        // address -> the meter NAK'd.) Attribute to whichever bank we are currently probing.
        uint32_t ptr = ((uint32_t)d[9]<<24)|((uint32_t)d[10]<<16)|((uint32_t)d[11]<<8)|d[12];
        g.probe_ptr = ptr; g.newest_ts = ts; g.probe_got = true; g.meta_seen = true;
        return;
    }
    uint16_t ptr = ((uint16_t)d[11]<<8)|d[12];     // meter_pro: unchanged 2-byte pointer
    if (!g.meta_seen || ptr > g.newest_ptr) { g.newest_ptr = ptr; g.newest_ts = ts; }
    if (!g.meta_seen || ptr < g.oldest_ptr) { g.oldest_ptr = ptr; g.oldest_ts = ts; }
    g.meta_seen = true;
}

// ── GATT callbacks ──────────────────────────────────────────────────────────────
static int on_write(uint16_t conn, const struct ble_gatt_error *err, struct ble_gatt_attr *attr, void *arg) {
    s_write_status = err ? err->status : 0;
    xSemaphoreGive(s_write_sem);
    return 0;
}
// blocking write (serialises GATT procedures; the device can't keep up with a flood)
static int write_blocking(uint16_t handle, const void *data, uint16_t len) {
    int rc = ble_gattc_write_flat(g.conn, handle, data, len, on_write, NULL);
    if (rc != 0) return rc;
    if (xSemaphoreTake(s_write_sem, pdMS_TO_TICKS(4000)) != pdTRUE) return BLE_HS_ETIMEOUT;
    return s_write_status;
}

static int on_chr(uint16_t conn, const struct ble_gatt_error *err, const struct ble_gatt_chr *chr, void *arg) {
    if (chr) {
        char u[BLE_UUID_STR_LEN]; ble_uuid_to_str(&chr->uuid.u, u);
        ha_mqtt_log("  chr val=%u %s", chr->val_handle, u);
        if (ble_uuid_cmp(&chr->uuid.u, &CMD_UUID.u) == 0) g.cmd_handle = chr->val_handle;
        else if (ble_uuid_cmp(&chr->uuid.u, &NOTIFY_UUID.u) == 0) g.notify_handle = chr->val_handle;
        return 0;
    }
    if (err && err->status == BLE_HS_EDONE) { xEventGroupSetBits(s_evt, BIT_DISCOVERED); return 0; }
    xEventGroupSetBits(s_evt, BIT_FAIL);
    return 0;
}
static int on_svc(uint16_t conn, const struct ble_gatt_error *err, const struct ble_gatt_svc *svc, void *arg) {
    if (svc) {
        char u[BLE_UUID_STR_LEN]; ble_uuid_to_str(&svc->uuid.u, u);
        ha_mqtt_log("svc %u-%u %s", svc->start_handle, svc->end_handle, u);
        if (ble_uuid_cmp(&svc->uuid.u, &SVC_UUID.u) == 0) { g.svc_start = svc->start_handle; g.svc_end = svc->end_handle; }
        return 0;
    }
    if (err && err->status == BLE_HS_EDONE) {
        if (g.svc_start) ble_gattc_disc_all_chrs(g.conn, g.svc_start, g.svc_end, on_chr, NULL);
        else { ha_mqtt_log("target service NOT found"); xEventGroupSetBits(s_evt, BIT_FAIL); }
        return 0;
    }
    ha_mqtt_log("svc disc err=%d", err ? err->status : -1);
    xEventGroupSetBits(s_evt, BIT_FAIL);
    return 0;
}

static int conn_event(struct ble_gap_event *event, void *arg) {
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        ha_mqtt_log("connect status=%d", event->connect.status);
        if (event->connect.status == 0) {
            g.conn = event->connect.conn_handle;
            xEventGroupSetBits(s_evt, BIT_CONNECTED);   // discovery is driven by pull_task (after settle)
        } else {
            xEventGroupSetBits(s_evt, BIT_FAIL);
        }
        return 0;
    case BLE_GAP_EVENT_DISCONNECT:
        ha_mqtt_log("disconnect reason=%d", event->disconnect.reason);
        xEventGroupSetBits(s_evt, BIT_DISCONNECT);
        return 0;
    case BLE_GAP_EVENT_NOTIFY_RX: {
        if (event->notify_rx.attr_handle != g.notify_handle) return 0;
        uint8_t buf[32];
        int len = OS_MBUF_PKTLEN(event->notify_rx.om);
        if (len > (int)sizeof(buf)) len = sizeof(buf);
        ble_hs_mbuf_to_flat(event->notify_rx.om, buf, len, NULL);
        if (g.diag && len <= 16) {
            char h[40]; int j = 0;
            for (int i = 0; i < len; i++) j += snprintf(h + j, sizeof(h) - j, "%02x", buf[i]);
            ha_mqtt_log("notif[%d]=%s", len, h);
        }
        if (len == 15) parse_meta(buf, len);
        else if (len == 16 && buf[0] == 0x01) relay_record(buf, len);
        return 0;
    }
    default: return 0;
    }
}

// ── Pull orchestration task ─────────────────────────────────────────────────────
static void publish_meta(void) {
    char p[256];
    if (g.prof == &PROF_OUTDOOR) {
        // synthesize a complete, consistent meta: oldest_ptr=start_addr, newest_ptr=newest_addr,
        // oldest_ts = newest_ts - span*interval  => server interval = (nt-ot)/(np-op) = HIST_INTERVAL_S.
        uint32_t span = g.newest_addr - g.start_addr;
        uint32_t oldest_ts = g.newest_ts - span * HIST_INTERVAL_S;
        snprintf(p, sizeof(p),
            "{\"t\":\"meta\",\"mac\":\"%s\",\"newest_ts\":%u,\"newest_ptr\":%u,\"oldest_ts\":%u,"
            "\"oldest_ptr\":%u,\"start_addr\":%u,\"bank\":%u,\"pull_now\":%u}",
            g.mac_str, (unsigned)g.newest_ts, (unsigned)g.newest_addr, (unsigned)oldest_ts,
            (unsigned)g.start_addr, (unsigned)g.start_addr, g.best_bank, (unsigned)g.pull_now);
    } else {
        snprintf(p, sizeof(p),
            "{\"t\":\"meta\",\"mac\":\"%s\",\"newest_ts\":%u,\"newest_ptr\":%u,\"oldest_ts\":%u,"
            "\"oldest_ptr\":%u,\"start_addr\":%u,\"pull_now\":%u}",
            g.mac_str, (unsigned)g.newest_ts, g.newest_ptr, (unsigned)g.oldest_ts,
            g.oldest_ptr, g.oldest_ptr, (unsigned)g.pull_now);
    }
    ha_mqtt_publish_history(g.mac_str, p);
}

static void publish_done(void);   // defined below; outdoor_pull() uses it

// Outdoor multi-bank pull (ADR-0009, 2026-06-22). Probe 570f3b00..03, pick the bank whose 0x69
// metadata carries the largest non-zero newest pointer, then page reads 570f3c<bank>00<addr:3 BE>06
// over a recent window ending at that pointer (gap-fill). Returns the finish() reason.
static const char *outdoor_pull(void) {
    write_blocking(g.cmd_handle, od_s0, sizeof od_s0);          // 570f3a primer
    vTaskDelay(pdMS_TO_TICKS(300));

    int best_bank = -1; uint32_t best = 0;
    for (int bank = 0; bank <= 3; bank++) {
        g.probe_got = false; g.probe_ptr = 0;
        uint8_t pb[4] = {0x57, 0x0f, 0x3b, (uint8_t)bank};
        write_blocking(g.cmd_handle, pb, sizeof pb);
        vTaskDelay(pdMS_TO_TICKS(900));                        // let the 0x69 metadata land
        ha_mqtt_log("bank %d: meta=%d ptr=%u", bank, (int)g.probe_got, (unsigned)g.probe_ptr);
        if (g.probe_got && g.probe_ptr > best) { best = g.probe_ptr; best_bank = bank; }
    }
    g.diag = false;
    if (best_bank < 0 || best == 0) return "no populated history bank";
    g.best_bank = (uint8_t)best_bank; g.newest_addr = best;
    ha_mqtt_log("outdoor bank=%d newest_addr=%u", best_bank, (unsigned)best);

    const uint32_t WINDOW = 4096u * REC_STRIDE;                 // cap recent-window depth (~4096 records)
    uint32_t start = (g.newest_addr > WINDOW) ? (g.newest_addr - WINDOW) : 0;
    start -= start % REC_STRIDE;
    g.start_addr = start;                                       // sample[0] address (publish_meta needs it)
    publish_meta();
    ESP_LOGI(TAG, "paging bank %d %u..%u", best_bank, (unsigned)start, (unsigned)g.newest_addr);
    uint8_t cmd[9] = {0x57, 0x0f, 0x3c, (uint8_t)best_bank, 0x00, 0, 0, 0, 0x06};
    for (uint32_t addr = start; addr <= g.newest_addr; addr += REC_STRIDE) {
        cmd[5] = (addr >> 16) & 0xff; cmd[6] = (addr >> 8) & 0xff; cmd[7] = addr & 0xff;
        if (write_blocking(g.cmd_handle, cmd, sizeof cmd) != 0) ESP_LOGW(TAG, "read fail @%u", (unsigned)addr);
        vTaskDelay(pdMS_TO_TICKS(8));
    }
    vTaskDelay(pdMS_TO_TICKS(1500));                            // flush tail notifications
    flush_remaining();
    publish_done();
    return "done";
}
static void publish_done(void) {
    char p[96];
    snprintf(p, sizeof(p), "{\"t\":\"done\",\"mac\":\"%s\",\"count\":%d}", g.mac_str, g.total_count);
    ha_mqtt_publish_history(g.mac_str, p);
}

static void finish(const char *why) {
    ha_mqtt_log("pull end: %s (relayed %d notifs)", why, g.total_count);
    ble_gap_terminate(g.conn, BLE_ERR_REM_USER_CONN_TERM);
    xEventGroupWaitBits(s_evt, BIT_DISCONNECT, pdTRUE, pdFALSE, pdMS_TO_TICKS(3000));
    ha_ble_scan_resume();
    s_busy = false;
}

static void pull_task(void *arg) {
    // 1) wait for the link, then settle before discovery (avoids ENOTCONN race)
    EventBits_t bits = xEventGroupWaitBits(s_evt, BIT_CONNECTED | BIT_FAIL | BIT_DISCONNECT,
                                           pdTRUE, pdFALSE, pdMS_TO_TICKS(12000));
    if (!(bits & BIT_CONNECTED)) { finish("connect failed"); vTaskDelete(NULL); return; }
    vTaskDelay(pdMS_TO_TICKS(400));

    // 2) discover services/characteristics (retry once on the transient ENOTCONN)
    for (int attempt = 0; attempt < 2; attempt++) {
        g.svc_start = 0; g.cmd_handle = 0; g.notify_handle = 0;
        ble_gattc_disc_all_svcs(g.conn, on_svc, NULL);
        bits = xEventGroupWaitBits(s_evt, BIT_DISCOVERED | BIT_FAIL | BIT_DISCONNECT,
                                   pdTRUE, pdFALSE, pdMS_TO_TICKS(8000));
        if (bits & BIT_DISCONNECT) { finish("disconnected during discovery"); vTaskDelete(NULL); return; }
        if ((bits & BIT_DISCOVERED) && g.cmd_handle && g.notify_handle) break;
        vTaskDelay(pdMS_TO_TICKS(400));
    }
    if (!g.cmd_handle || !g.notify_handle) {
        ha_mqtt_log("discovery fail: cmd=%u notify=%u", g.cmd_handle, g.notify_handle);
        finish("discovery failed"); vTaskDelete(NULL); return;
    }
    ha_mqtt_log("discovered: cmd=%u notify=%u", g.cmd_handle, g.notify_handle);

    // subscribe to notifications (CCCD = notify value handle + 1)
    uint8_t cccd[2] = {0x01, 0x00};
    if (write_blocking(g.notify_handle + 1, cccd, 2) != 0) { finish("subscribe failed"); vTaskDelete(NULL); return; }

    // handshake (prefix + current unix time, BE)
    g.pull_now = (uint32_t)time(NULL);
    uint8_t hs[sizeof(HANDSHAKE_PREFIX) + 4];
    memcpy(hs, HANDSHAKE_PREFIX, sizeof(HANDSHAKE_PREFIX));
    hs[9]=(g.pull_now>>24)&0xff; hs[10]=(g.pull_now>>16)&0xff; hs[11]=(g.pull_now>>8)&0xff; hs[12]=g.pull_now&0xff;
    write_blocking(g.cmd_handle, hs, sizeof(hs));
    vTaskDelay(pdMS_TO_TICKS(300));

    // Outdoor meters use a multi-bank store with a different read format — bank-discovery path.
    if (g.prof == &PROF_OUTDOOR) {
        const char *r = outdoor_pull();
        ESP_LOGI(TAG, "outdoor pull: %s (%d notifs)", r, g.total_count);
        finish(r);
        vTaskDelete(NULL);
        return;
    }

    // setup + metadata, retried: the oldest-pointer notification can be slow or missed on the
    // first pass (esp. on Outdoor meters), so re-issue the setup until both pointers appear.
    for (int attempt = 0; attempt < 4; attempt++) {
        for (int i = 0; i < g.prof->n_setup; i++) {
            write_blocking(g.cmd_handle, g.prof->setup[i].bytes, g.prof->setup[i].len);
            vTaskDelay(pdMS_TO_TICKS(300));
        }
        vTaskDelay(pdMS_TO_TICKS(2000));
        ha_mqtt_log("meta try %d: count=%d newest=%u oldest=%u", attempt, g.meta_count, g.newest_ptr, g.oldest_ptr);
        if (g.meta_seen && g.newest_ptr > g.oldest_ptr) break;
    }
    g.diag = false;   // stop per-notification logging before the (high-volume) paging phase
    if (!g.meta_seen || g.newest_ptr <= g.oldest_ptr) { finish("no/!bad metadata"); vTaskDelete(NULL); return; }
    publish_meta();
    ESP_LOGI(TAG, "paging %u..%u (%u recs)", g.oldest_ptr, g.newest_ptr, (g.newest_ptr-g.oldest_ptr)/REC_STRIDE);

    // page: write read commands for each address window
    uint8_t cmd[16];
    int rp = g.prof->rp_len;
    memcpy(cmd, g.prof->read_prefix, rp);
    for (uint16_t addr = g.oldest_ptr; addr < g.newest_ptr; addr += REC_STRIDE) {
        cmd[rp]   = (addr >> 8) & 0xff;
        cmd[rp+1] = addr & 0xff;
        cmd[rp+2] = 0x06;
        if (write_blocking(g.cmd_handle, cmd, rp + 3) != 0) { ESP_LOGW(TAG, "read write failed @%u", addr); }
        vTaskDelay(pdMS_TO_TICKS(8));   // pacing; notifications stream in via conn_event
    }
    vTaskDelay(pdMS_TO_TICKS(1500));    // flush tail notifications

    flush_remaining();
    publish_done();
    ESP_LOGI(TAG, "pull complete: %d record notifications relayed", g.total_count);
    finish("done");
    vTaskDelete(NULL);
}

bool gatt_history_pull(const char *mac_str, const char *profile) {
    if (s_busy) { ESP_LOGW(TAG, "busy; ignoring pull for %s", mac_str); return false; }
    if (!s_evt) { s_evt = xEventGroupCreate(); s_write_sem = xSemaphoreCreateBinary(); s_batch_mutex = xSemaphoreCreateMutex(); }

    memset(&g, 0, sizeof(g));
    g.diag = true;   // log each notification through the metadata phase
    snprintf(g.mac_str, sizeof(g.mac_str), "%s", mac_str);
    g.prof = (profile && strcmp(profile, "meter_pro") == 0) ? &PROF_METER_PRO : &PROF_OUTDOOR;

    if (!ha_ble_lookup_addr(mac_str, &g.addr)) {
        ha_mqtt_log("pull %s: addr not cached by scanner — can't connect", mac_str);
        return false;
    }
    s_busy = true;
    xEventGroupClearBits(s_evt, 0xFF);
    ha_ble_scan_pause();

    ha_mqtt_log("pull %s: connecting (addr_type=%d, profile=%s)", mac_str, g.addr.type,
                g.prof == &PROF_METER_PRO ? "meter_pro" : "outdoor");
    int rc = ble_gap_connect(ha_ble_own_addr_type(), &g.addr, 10000, NULL, conn_event, NULL);
    if (rc != 0) {
        ha_mqtt_log("pull %s: ble_gap_connect rc=%d", mac_str, rc);
        ha_ble_scan_resume(); s_busy = false; return false;
    }
    xTaskCreate(pull_task, "sb_pull", 6144, NULL, 5, NULL);
    return true;
}
