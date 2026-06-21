// Generic GATT step-interpreter — see gatt_exec.h. Connect → discover-all-chars → run server-composed
// steps → stream replies. Reuses the radio-sharing, blocking-write, and batched-relay patterns proven
// in gatt_history.c. Single radio: the passive scan is paused for the duration and resumed on exit.
#include "gatt_exec.h"
#include "ble_scan.h"
#include "ha_mqtt.h"
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "host/ble_hs.h"
#include "host/ble_gap.h"
#include "host/ble_gatt.h"
#include "cJSON.h"

static const char *TAG = "gatt_exec";

// ── Limits (one central op at a time → static storage is safe) ───────────────────
#define GE_MAX_STEPS 24
#define GE_MAX_PAYLEN 40     // bytes per write payload
#define GE_MAX_SEQ    8      // writes inside a writeseq
#define GE_MAX_CHRS   28     // discovered characteristics mapped
#define GE_BATCH_N    16     // notifs per relayed batch

typedef enum { GE_SUB, GE_WRITE, GE_WRITESEQ, GE_READ, GE_COLLECT, GE_DELAY } ge_op_t;

typedef struct {
    ge_op_t op;
    ble_uuid_any_t chr;                       // target characteristic (sub/write/writeseq/read)
    bool has_chr;
    uint8_t pay[GE_MAX_SEQ][GE_MAX_PAYLEN];
    uint8_t plen[GE_MAX_SEQ];
    int nseq;                                 // payload count (1 for write, N for writeseq)
    int gap_ms;                               // inter-write gap (writeseq)
    int ms;                                   // dwell (collect/delay)
} ge_step_t;

// ── Event bits / sync ────────────────────────────────────────────────────────────
#define BIT_DISCOVERED  BIT0
#define BIT_FAIL        BIT1
#define BIT_DISCONNECT  BIT2
#define BIT_CONNECTED   BIT3

static volatile bool s_busy;
static EventGroupHandle_t s_evt;
static SemaphoreHandle_t  s_write_sem;
static SemaphoreHandle_t  s_batch_mutex;
static volatile int s_write_status;

static struct {
    char reqid[24];
    char mac_str[18];
    ble_addr_t addr;
    uint16_t conn;
    ge_step_t steps[GE_MAX_STEPS];
    int n_steps;
    // discovered characteristics
    struct { ble_uuid_any_t uuid; uint16_t handle; } chr[GE_MAX_CHRS];
    int n_chr;
    // notif relay batch (guarded by s_batch_mutex)
    char batch[1024];
    int batch_count, total_notif, seq;
} g;

bool gatt_exec_busy(void) { return s_busy; }

// ── helpers ──────────────────────────────────────────────────────────────────────
static int hex2bin(const char *hex, uint8_t *out, int max) {
    int n = 0;
    for (const char *p = hex; p[0] && p[1] && n < max; p += 2) {
        char b[3] = { p[0], p[1], 0 };
        out[n++] = (uint8_t)strtol(b, NULL, 16);
    }
    return n;
}

static uint16_t handle_for(const ble_uuid_t *u) {
    for (int i = 0; i < g.n_chr; i++)
        if (ble_uuid_cmp(&g.chr[i].uuid.u, u) == 0) return g.chr[i].handle;
    return 0;
}

// ── reply publishing (topic = home/edge/<node>/<reqid>/reply) ────────────────────
static void reply(const char *payload) { ha_mqtt_publish_reply(g.reqid, payload); }

static void publish_open(void) {
    char p[768]; int j = 0;
    j += snprintf(p + j, sizeof(p) - j, "{\"t\":\"open\",\"mac\":\"%s\",\"chrs\":[", g.mac_str);
    for (int i = 0; i < g.n_chr && j < (int)sizeof(p) - 80; i++) {
        char u[BLE_UUID_STR_LEN]; ble_uuid_to_str(&g.chr[i].uuid.u, u);
        j += snprintf(p + j, sizeof(p) - j, "%s{\"u\":\"%s\",\"h\":%u}",
                      i ? "," : "", u, g.chr[i].handle);
    }
    snprintf(p + j, sizeof(p) - j, "]}");
    reply(p);
}

// Serialise the notif batch into out[]; caller holds s_batch_mutex and publishes AFTER releasing it.
static bool take_batch(char *out, int out_sz) {
    if (g.batch_count == 0) return false;
    int n = snprintf(out, out_sz, "{\"t\":\"notif\",\"seq\":%d,\"items\":[%s]}", g.seq++, g.batch);
    g.batch[0] = '\0'; g.batch_count = 0;
    return n > 0 && n < out_sz;
}
static void relay_notif(uint16_t handle, const uint8_t *data, int len) {
    char hex[2 * 32 + 1] = ""; int j = 0;   // init: a zero-length value must yield "" not stack garbage
    for (int i = 0; i < len && i < 32; i++) j += snprintf(hex + j, sizeof(hex) - j, "%02x", data[i]);
    char payload[1024]; bool send = false;
    xSemaphoreTake(s_batch_mutex, portMAX_DELAY);
    if (g.batch_count) strlcat(g.batch, ",", sizeof(g.batch));
    char item[80]; snprintf(item, sizeof(item), "[%u,\"%s\"]", handle, hex);
    strlcat(g.batch, item, sizeof(g.batch));
    g.batch_count++; g.total_notif++;
    if (g.batch_count >= GE_BATCH_N) send = take_batch(payload, sizeof(payload));
    xSemaphoreGive(s_batch_mutex);
    if (send) reply(payload);
}
static void flush_notifs(void) {
    char payload[1024]; bool send;
    xSemaphoreTake(s_batch_mutex, portMAX_DELAY);
    send = take_batch(payload, sizeof(payload));
    xSemaphoreGive(s_batch_mutex);
    if (send) reply(payload);
}

// ── GATT callbacks ───────────────────────────────────────────────────────────────
static int on_write(uint16_t conn, const struct ble_gatt_error *err, struct ble_gatt_attr *attr, void *arg) {
    s_write_status = err ? err->status : 0;
    xSemaphoreGive(s_write_sem);
    return 0;
}
static int write_blocking(uint16_t handle, const void *data, uint16_t len) {
    int rc = ble_gattc_write_flat(g.conn, handle, data, len, on_write, NULL);
    if (rc != 0) return rc;
    if (xSemaphoreTake(s_write_sem, pdMS_TO_TICKS(4000)) != pdTRUE) return BLE_HS_ETIMEOUT;
    return s_write_status;
}

static int s_read_status;
static uint8_t s_read_buf[64]; static int s_read_len;
static int on_read(uint16_t conn, const struct ble_gatt_error *err, struct ble_gatt_attr *attr, void *arg) {
    s_read_status = err ? err->status : 0;
    s_read_len = 0;
    if (!err && attr && attr->om) {
        s_read_len = OS_MBUF_PKTLEN(attr->om);
        if (s_read_len > (int)sizeof(s_read_buf)) s_read_len = sizeof(s_read_buf);
        ble_hs_mbuf_to_flat(attr->om, s_read_buf, s_read_len, NULL);
    }
    xSemaphoreGive(s_write_sem);    // reuse the write sem to block on a read
    return 0;
}

static int on_chr(uint16_t conn, const struct ble_gatt_error *err, const struct ble_gatt_chr *chr, void *arg) {
    if (chr) {
        if (g.n_chr < GE_MAX_CHRS) {
            memcpy(&g.chr[g.n_chr].uuid, &chr->uuid, sizeof(ble_uuid_any_t));
            g.chr[g.n_chr].handle = chr->val_handle;
            g.n_chr++;
        }
        return 0;
    }
    xEventGroupSetBits(s_evt, (err && err->status == BLE_HS_EDONE) ? BIT_DISCOVERED : BIT_FAIL);
    return 0;
}

static int conn_event(struct ble_gap_event *event, void *arg) {
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) { g.conn = event->connect.conn_handle; xEventGroupSetBits(s_evt, BIT_CONNECTED); }
        else xEventGroupSetBits(s_evt, BIT_FAIL);
        return 0;
    case BLE_GAP_EVENT_DISCONNECT:
        xEventGroupSetBits(s_evt, BIT_DISCONNECT);
        return 0;
    case BLE_GAP_EVENT_NOTIFY_RX: {
        uint8_t buf[32];
        int len = OS_MBUF_PKTLEN(event->notify_rx.om);
        if (len > (int)sizeof(buf)) len = sizeof(buf);
        ble_hs_mbuf_to_flat(event->notify_rx.om, buf, len, NULL);
        relay_notif(event->notify_rx.attr_handle, buf, len);
        return 0;
    }
    default: return 0;
    }
}

// ── step execution ───────────────────────────────────────────────────────────────
static void step_err(const char *msg) { char p[160]; snprintf(p, sizeof(p), "{\"t\":\"error\",\"msg\":\"%s\"}", msg); reply(p); }

static void run_step(const ge_step_t *st, int idx) {
    switch (st->op) {
    case GE_DELAY:
        vTaskDelay(pdMS_TO_TICKS(st->ms));
        break;
    case GE_COLLECT:
        flush_notifs();                      // checkpoint anything buffered, then dwell for notifs
        vTaskDelay(pdMS_TO_TICKS(st->ms));
        flush_notifs();
        break;
    case GE_SUB: {
        uint16_t h = handle_for(&st->chr.u);
        if (!h) { step_err("sub: unknown char"); break; }
        uint8_t cccd[2] = {0x01, 0x00};      // notifications on (CCCD = val_handle + 1, SwitchBot convention)
        int rc = write_blocking(h + 1, cccd, 2);
        char p[96]; snprintf(p, sizeof(p), "{\"t\":\"step\",\"i\":%d,\"op\":\"sub\",\"h\":%u,\"rc\":%d}", idx, h, rc); reply(p);
        break;
    }
    case GE_WRITE:
    case GE_WRITESEQ: {
        uint16_t h = handle_for(&st->chr.u);
        if (!h) { step_err("write: unknown char"); break; }
        int rc = 0;
        for (int k = 0; k < st->nseq; k++) {
            rc = write_blocking(h, st->pay[k], st->plen[k]);
            if (k + 1 < st->nseq && st->gap_ms) vTaskDelay(pdMS_TO_TICKS(st->gap_ms));
        }
        char p[112]; snprintf(p, sizeof(p), "{\"t\":\"step\",\"i\":%d,\"op\":\"write\",\"h\":%u,\"n\":%d,\"rc\":%d}",
                              idx, h, st->nseq, rc); reply(p);
        break;
    }
    case GE_READ: {
        uint16_t h = handle_for(&st->chr.u);
        if (!h) { step_err("read: unknown char"); break; }
        s_read_status = -1; s_read_len = 0;
        int rc = ble_gattc_read(g.conn, h, on_read, NULL);
        if (rc == 0 && xSemaphoreTake(s_write_sem, pdMS_TO_TICKS(4000)) == pdTRUE) {
            char hex[2 * 64 + 1] = ""; int j = 0;   // init: empty read value must yield "" not garbage
            for (int i = 0; i < s_read_len; i++) j += snprintf(hex + j, sizeof(hex) - j, "%02x", s_read_buf[i]);
            char p[200]; snprintf(p, sizeof(p), "{\"t\":\"read\",\"i\":%d,\"h\":%u,\"rc\":%d,\"d\":\"%s\"}",
                                  idx, h, s_read_status, hex); reply(p);
        } else {
            char p[96]; snprintf(p, sizeof(p), "{\"t\":\"read\",\"i\":%d,\"h\":%u,\"rc\":%d,\"d\":\"\"}", idx, h, rc ? rc : -1); reply(p);
        }
        break;
    }
    }
}

static void exec_task(void *arg) {
    EventBits_t bits = xEventGroupWaitBits(s_evt, BIT_CONNECTED | BIT_FAIL | BIT_DISCONNECT,
                                           pdTRUE, pdFALSE, pdMS_TO_TICKS(12000));
    if (!(bits & BIT_CONNECTED)) { step_err("connect failed"); goto done; }
    vTaskDelay(pdMS_TO_TICKS(400));          // settle (avoids ENOTCONN race, as in gatt_history)

    // discover ALL characteristics across the whole handle range (generic UUID→handle map)
    for (int attempt = 0; attempt < 2; attempt++) {
        g.n_chr = 0;
        ble_gattc_disc_all_chrs(g.conn, 0x0001, 0xffff, on_chr, NULL);
        bits = xEventGroupWaitBits(s_evt, BIT_DISCOVERED | BIT_FAIL | BIT_DISCONNECT, pdTRUE, pdFALSE, pdMS_TO_TICKS(8000));
        if (bits & BIT_DISCONNECT) { step_err("disconnected during discovery"); goto done; }
        if ((bits & BIT_DISCOVERED) && g.n_chr > 0) break;
        vTaskDelay(pdMS_TO_TICKS(400));
    }
    if (g.n_chr == 0) { step_err("no characteristics discovered"); goto done; }
    publish_open();

    for (int i = 0; i < g.n_steps; i++) {
        if (xEventGroupGetBits(s_evt) & BIT_DISCONNECT) { step_err("disconnected mid-run"); goto done; }
        run_step(&g.steps[i], i);
    }
    flush_notifs();

    {
        char p[96]; snprintf(p, sizeof(p), "{\"t\":\"done\",\"status\":0,\"notifs\":%d}", g.total_notif); reply(p);
    }
done:
    ble_gap_terminate(g.conn, BLE_ERR_REM_USER_CONN_TERM);
    xEventGroupWaitBits(s_evt, BIT_DISCONNECT, pdTRUE, pdFALSE, pdMS_TO_TICKS(3000));
    ha_ble_scan_resume();
    s_busy = false;
    vTaskDelete(NULL);
}

// ── parse + launch ───────────────────────────────────────────────────────────────
static bool parse_uuid(const char *s, ble_uuid_any_t *out) {
    // accept "16-bit" hex (e.g. "2a37") or full 128-bit canonical string
    int rc = ble_uuid_from_str(out, s);
    return rc == 0;
}

static int parse_steps(const char *json) {
    cJSON *arr = cJSON_Parse(json);
    if (!cJSON_IsArray(arr)) { cJSON_Delete(arr); return -1; }
    int n = 0;
    cJSON *it;
    cJSON_ArrayForEach(it, arr) {
        if (n >= GE_MAX_STEPS) break;
        ge_step_t *st = &g.steps[n];
        memset(st, 0, sizeof(*st));
        const cJSON *s = cJSON_GetObjectItem(it, "s");
        if (!cJSON_IsString(s)) continue;
        const cJSON *ch  = cJSON_GetObjectItem(it, "char");
        const cJSON *hex = cJSON_GetObjectItem(it, "hex");
        const cJSON *ms  = cJSON_GetObjectItem(it, "ms");
        const cJSON *gap = cJSON_GetObjectItem(it, "gap_ms");
        if (cJSON_IsString(ch)) st->has_chr = parse_uuid(ch->valuestring, &st->chr);

        if (!strcmp(s->valuestring, "sub"))         st->op = GE_SUB;
        else if (!strcmp(s->valuestring, "read"))   st->op = GE_READ;
        else if (!strcmp(s->valuestring, "delay"))  { st->op = GE_DELAY;   st->ms = cJSON_IsNumber(ms) ? ms->valueint : 100; }
        else if (!strcmp(s->valuestring, "collect")){ st->op = GE_COLLECT; st->ms = cJSON_IsNumber(ms) ? ms->valueint : 1000; }
        else if (!strcmp(s->valuestring, "write") || !strcmp(s->valuestring, "writeseq")) {
            st->op = strcmp(s->valuestring, "writeseq") ? GE_WRITE : GE_WRITESEQ;
            st->gap_ms = cJSON_IsNumber(gap) ? gap->valueint : 200;
            if (cJSON_IsString(hex)) {                       // single payload
                st->plen[0] = hex2bin(hex->valuestring, st->pay[0], GE_MAX_PAYLEN); st->nseq = 1;
            } else if (cJSON_IsArray(hex)) {                 // sequence of payloads
                const cJSON *h; int k = 0;
                cJSON_ArrayForEach(h, hex) {
                    if (k >= GE_MAX_SEQ || !cJSON_IsString(h)) break;
                    st->plen[k] = hex2bin(h->valuestring, st->pay[k], GE_MAX_PAYLEN); k++;
                }
                st->nseq = k;
            }
        } else continue;                                     // unknown step → skip
        n++;
    }
    cJSON_Delete(arr);
    return n;
}

bool gatt_exec_run(const char *reqid, const char *mac_str, const char *steps_json) {
    if (s_busy) { ESP_LOGW(TAG, "busy; ignoring gatt exec for %s", mac_str); return false; }
    if (!s_evt) { s_evt = xEventGroupCreate(); s_write_sem = xSemaphoreCreateBinary(); s_batch_mutex = xSemaphoreCreateMutex(); }

    memset(&g, 0, sizeof(g));
    snprintf(g.reqid, sizeof(g.reqid), "%s", reqid ? reqid : "0");
    snprintf(g.mac_str, sizeof(g.mac_str), "%s", mac_str);

    g.n_steps = parse_steps(steps_json);   // 0 = empty list (valid: probe = connect+discover only)
    if (g.n_steps < 0) { ha_mqtt_log("gatt exec %s: malformed steps", mac_str); return false; }

    if (!ha_ble_lookup_addr(mac_str, &g.addr)) {
        ha_mqtt_log("gatt exec %s: addr not cached by scanner — can't connect", mac_str);
        return false;
    }
    s_busy = true;
    xEventGroupClearBits(s_evt, 0xFF);
    ha_ble_scan_pause();

    ha_mqtt_log("gatt exec %s: reqid=%s steps=%d connecting", mac_str, g.reqid, g.n_steps);
    int rc = ble_gap_connect(ha_ble_own_addr_type(), &g.addr, 10000, NULL, conn_event, NULL);
    if (rc != 0) {
        ha_mqtt_log("gatt exec %s: ble_gap_connect rc=%d", mac_str, rc);
        ha_ble_scan_resume(); s_busy = false; return false;
    }
    xTaskCreate(exec_task, "gatt_exec", 6144, NULL, 5, NULL);
    return true;
}
