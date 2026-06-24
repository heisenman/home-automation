// Relay coverage allowlist — see ha_relay.h. The directive arrives signed+verified (ha_mqtt verifies the
// {p,s} HMAC before calling ha_relay_apply); this module only parses the inner relay_assign, epoch-guards
// it, applies the filter, and persists to NVS. Read from the BLE host task (ha_relay_allowed), written from
// the MQTT task (ha_relay_apply) — short critical sections guard the shared allowlist.
//
// relay_macs semantics:  omitted  -> relay-ALL (reset; the default state)
//                        []       -> relay-NONE (node explicitly relays nothing)
//                        [A,B,..] -> relay only those MACs
#include "ha_relay.h"
#include "freertos/FreeRTOS.h"
#include "nvs.h"
#include "cJSON.h"
#include "esp_log.h"
#include <string.h>

static const char *TAG = "ha_relay";
#define MAX_RELAY 48           // plenty for a single node's coverage set; extra entries are dropped (logged)
#define NVS_NS    "harelay"

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static bool   s_filter;                     // false = relay-all (default / reset); true = use allowlist
static int    s_epoch = -1;                 // last applied epoch (-1 = none yet); older/equal = ignored
static int    s_count;
static char   s_macs[MAX_RELAY][13];        // normalized: lower-case, colon-stripped, 12 chars + NUL

// "AA:BB:..".."aabb.." -> lower-case colon-stripped 12-char into out[13].
static void norm_mac(const char *in, char out[13]) {
    int j = 0;
    for (const char *p = in; *p && j < 12; ++p) {
        if (*p == ':') continue;
        char ch = *p;
        out[j++] = (ch >= 'A' && ch <= 'Z') ? (char)(ch + 32) : ch;
    }
    out[j] = '\0';
}

static void persist(void) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) { ESP_LOGW(TAG, "NVS open(rw) failed"); return; }
    nvs_set_u8(h, "filter", s_filter ? 1 : 0);
    nvs_set_i32(h, "epoch", s_epoch);
    nvs_set_u8(h, "n", (uint8_t)s_count);
    if (s_count) nvs_set_blob(h, "macs", s_macs, (size_t)s_count * 13);
    nvs_commit(h);
    nvs_close(h);
}

void ha_relay_init(void) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        ESP_LOGI(TAG, "no persisted directive — relay-all default");
        return;
    }
    int32_t e = -1; if (nvs_get_i32(h, "epoch", &e) == ESP_OK) s_epoch = (int)e;
    uint8_t filter = 0; nvs_get_u8(h, "filter", &filter);
    if (filter) {
        uint8_t n = 0; nvs_get_u8(h, "n", &n); s_count = (n > MAX_RELAY) ? MAX_RELAY : n;
        size_t sz = sizeof(s_macs);
        if (s_count && nvs_get_blob(h, "macs", s_macs, &sz) == ESP_OK) s_filter = true;
        else if (s_count == 0) s_filter = true;     // persisted relay-NONE
    }
    nvs_close(h);
    ESP_LOGI(TAG, "loaded: filter=%d epoch=%d count=%d", s_filter, s_epoch, s_count);
}

bool ha_relay_allowed(const char *mac_str) {
    char m[13]; norm_mac(mac_str, m);
    bool ok;
    portENTER_CRITICAL(&s_mux);
    if (!s_filter) {
        ok = true;                                   // relay-all until the dictator says otherwise
    } else {
        ok = false;
        for (int i = 0; i < s_count; i++) {
            if (memcmp(s_macs[i], m, 13) == 0) { ok = true; break; }
        }
    }
    portEXIT_CRITICAL(&s_mux);
    return ok;
}

void ha_relay_apply(const char *json) {
    cJSON *root = cJSON_Parse(json);
    if (!root) { ESP_LOGW(TAG, "relay_assign: bad json"); return; }
    const cJSON *type = cJSON_GetObjectItem(root, "type");
    const cJSON *ep   = cJSON_GetObjectItem(root, "epoch");
    const cJSON *macs = cJSON_GetObjectItem(root, "relay_macs");
    if (!cJSON_IsString(type) || strcmp(type->valuestring, "relay_assign") != 0) {
        ESP_LOGW(TAG, "ignoring: not a relay_assign"); cJSON_Delete(root); return;
    }
    int epoch = cJSON_IsNumber(ep) ? (int)ep->valuedouble : 0;
    if (s_epoch >= 0 && epoch <= s_epoch) {          // epoch guard → idempotent + ordered, replay-safe
        ESP_LOGI(TAG, "ignoring stale relay_assign epoch=%d (have %d)", epoch, s_epoch);
        cJSON_Delete(root); return;
    }

    if (!macs) {                                     // relay_macs omitted → relay-ALL reset
        portENTER_CRITICAL(&s_mux);
        s_filter = false; s_count = 0; s_epoch = epoch;
        portEXIT_CRITICAL(&s_mux);
        persist();
        ESP_LOGI(TAG, "applied relay_assign epoch=%d → relay-ALL (reset)", epoch);
        cJSON_Delete(root); return;
    }

    // Build the new list outside the lock (parsing/normalizing is the slow part).
    char next[MAX_RELAY][13]; int n = 0;
    if (cJSON_IsArray(macs)) {
        const cJSON *it;
        cJSON_ArrayForEach(it, macs) {
            if (!cJSON_IsString(it)) continue;
            if (n >= MAX_RELAY) { ESP_LOGW(TAG, "relay_assign > %d MACs — extra dropped", MAX_RELAY); break; }
            norm_mac(it->valuestring, next[n++]);
        }
    }
    portENTER_CRITICAL(&s_mux);
    s_count = n;
    for (int i = 0; i < n; i++) memcpy(s_macs[i], next[i], 13);
    s_filter = true;
    s_epoch = epoch;
    portEXIT_CRITICAL(&s_mux);

    persist();
    ESP_LOGI(TAG, "applied relay_assign epoch=%d → %s%d MAC(s)",
             epoch, n == 0 ? "relay-NONE, " : "", n);
    cJSON_Delete(root);
    // NOTE: `cmd_relay` (actuator device_ids this node relays commands to) is carried in the directive but
    // not yet acted on — no BLE actuator sits behind an edge node today (ADR-0015 Tier-2 downlink, future).
}
