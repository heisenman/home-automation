#include "ha_mqtt.h"
#include "ha_sntp.h"
#include "gatt_history.h"
#include "gatt_exec.h"
#include "ha_ota.h"
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include "mqtt_client.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "cJSON.h"
#include <time.h>
#include "mbedtls/md.h"
#if __has_include("secrets.h")
#include "secrets.h"
#endif
#ifndef HA_CMD_SECRET
#define HA_CMD_SECRET ""        // empty → all signed commands rejected (must provision a secret)
#endif
#ifndef HA_MQTT_USER
#define HA_MQTT_USER ""         // empty → anonymous (latent until broker auth cutover)
#endif
#ifndef HA_MQTT_PASS
#define HA_MQTT_PASS ""
#endif

#ifndef HA_FW_VERSION
#define HA_FW_VERSION "v5-histbank"  // bump to prove an OTA swapped the running image
#endif

static const char *TAG = "ha_mqtt";
static esp_mqtt_client_handle_t s_client;
static char s_node[32];
static char s_status_topic[64];
static char s_cmd_topic[64];
static char s_online_msg[64];     // "online <slot> <fwver>" — shows which OTA slot/version is running
static volatile bool s_connected;

// lower-case, colon-stripped MAC into dst (>=13 bytes)
static void macflat(const char *mac_str, char *dst) {
    int j = 0;
    for (const char *p = mac_str; *p && j < 12; ++p) {
        if (*p == ':') continue;
        char c = *p;
        dst[j++] = (c >= 'A' && c <= 'Z') ? (c + 32) : c;
    }
    dst[j] = '\0';
}

// HMAC-SHA256(secret, p) == sig_hex ?  Verifies a signed-envelope command (ADR-0010). We hash the
// LITERAL p string the server signed (cJSON returns it un-escaped, a faithful round-trip), so there
// is no canonicalisation mismatch between the Python signer and this verifier.
static bool cmd_sig_ok(const char *p, const char *sig_hex) {
    if (!HA_CMD_SECRET[0] || strlen(sig_hex) != 64) return false;
    const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    if (!info) return false;
    unsigned char mac[32];
    if (mbedtls_md_hmac(info, (const unsigned char *)HA_CMD_SECRET, strlen(HA_CMD_SECRET),
                        (const unsigned char *)p, strlen(p), mac) != 0) return false;
    char hex[65];
    for (int i = 0; i < 32; i++) snprintf(hex + i * 2, 3, "%02x", mac[i]);
    unsigned char diff = 0;                       // constant-time compare
    for (int i = 0; i < 64; i++) diff |= (unsigned char)(hex[i] ^ sig_hex[i]);
    return diff == 0;
}

// Dispatch a verified command object (op/mac/steps/url). Caller owns the cJSON.
static void dispatch_cmd(const cJSON *cmd) {
    const cJSON *op = cJSON_GetObjectItem(cmd, "op");
    const cJSON *mac = cJSON_GetObjectItem(cmd, "mac");
    const cJSON *prof = cJSON_GetObjectItem(cmd, "profile");
    if (cJSON_IsString(op) && strcmp(op->valuestring, "history") == 0 && cJSON_IsString(mac)) {
        const char *profile = cJSON_IsString(prof) ? prof->valuestring : "outdoor";
        ESP_LOGI(TAG, "cmd: history pull mac=%s profile=%s", mac->valuestring, profile);
        if (gatt_exec_busy()) ESP_LOGW(TAG, "central busy; dropping history pull");
        else gatt_history_pull(mac->valuestring, profile);
    } else if (cJSON_IsString(op) && strcmp(op->valuestring, "gatt") == 0 && cJSON_IsString(mac)) {
        // Generic GATT forwarder: {"op":"gatt","reqid":"..","mac":"..","steps":[...]}
        const cJSON *reqid = cJSON_GetObjectItem(cmd, "reqid");
        const cJSON *steps = cJSON_GetObjectItem(cmd, "steps");
        if (!cJSON_IsArray(steps)) { ESP_LOGW(TAG, "gatt cmd: missing steps[]"); return; }
        char *steps_json = cJSON_PrintUnformatted(steps);   // re-serialise just the steps array
        const char *rid = cJSON_IsString(reqid) ? reqid->valuestring : "0";
        ESP_LOGI(TAG, "cmd: gatt exec mac=%s reqid=%s", mac->valuestring, rid);
        if (gatt_history_busy() || gatt_exec_busy()) ESP_LOGW(TAG, "central busy; dropping gatt exec");
        else if (steps_json) gatt_exec_run(rid, mac->valuestring, steps_json);
        if (steps_json) cJSON_free(steps_json);
    } else if (cJSON_IsString(op) && strcmp(op->valuestring, "ota") == 0) {
        // Firmware OTA: {"op":"ota","url":"http://<server>:<port>/ha-edge-c6.bin"}
        const cJSON *url = cJSON_GetObjectItem(cmd, "url");
        const cJSON *sha = cJSON_GetObjectItem(cmd, "sha256");
        if (cJSON_IsString(url)) {
            ESP_LOGI(TAG, "cmd: ota url=%s", url->valuestring);
            ha_ota_start(url->valuestring, cJSON_IsString(sha) ? sha->valuestring : NULL);
        } else ESP_LOGW(TAG, "ota cmd: missing url");
    } else {
        ESP_LOGW(TAG, "unknown/!malformed cmd");
    }
}

static void handle_cmd(const char *data, int len) {
    cJSON *root = cJSON_ParseWithLength(data, len);
    if (!root) { ESP_LOGW(TAG, "bad cmd json"); return; }

    cJSON *inner = NULL;
    const cJSON *cmd = root;
    const cJSON *p = cJSON_GetObjectItem(root, "p");
    const cJSON *s = cJSON_GetObjectItem(root, "s");
    if (cJSON_IsString(p) && cJSON_IsString(s)) {
        // Signed envelope {p,s}: verify HMAC over the literal p string, then act on the inner cmd.
        if (!cmd_sig_ok(p->valuestring, s->valuestring)) {
            ha_mqtt_log("cmd rejected: bad-sig"); cJSON_Delete(root); return;
        }
        inner = cJSON_Parse(p->valuestring);
        if (!inner) { ESP_LOGW(TAG, "cmd: bad inner json"); cJSON_Delete(root); return; }
        // Freshness window: tight for actuation/gatt (replay defense), but WIDE for ota so a node whose
        // clock has drifted (the C6 RTC does) can still be OTA-recovered — the OTA directive is signed +
        // image-hash-verified + version anti-downgrade, so a replay just re-flashes the same image.
        const cJSON *op = cJSON_GetObjectItem(inner, "op");
        long window = (cJSON_IsString(op) && strcmp(op->valuestring, "ota") == 0) ? 86400 : 300;
        const cJSON *ts = cJSON_GetObjectItem(inner, "ts");      // freshness (clock is SNTP-synced)
        if (cJSON_IsNumber(ts)) {
            long dt = (long)time(NULL) - (long)ts->valuedouble;
            if (dt < -window || dt > window) {
                ha_mqtt_log("cmd rejected: stale (dt=%lds win=%lds)", dt, window);
                cJSON_Delete(inner); cJSON_Delete(root); return;
            }
        }
        cmd = inner;
    } else {
        // Signature now REQUIRED for every op, including ota (the unsigned recovery exception is gone).
        ha_mqtt_log("cmd rejected: unsigned (signature required)");
        cJSON_Delete(root); return;
    }

    dispatch_cmd(cmd);
    if (inner) cJSON_Delete(inner);
    cJSON_Delete(root);
}

static void on_mqtt(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data) {
    esp_mqtt_event_handle_t e = event_data;
    switch ((esp_mqtt_event_id_t)event_id) {
        case MQTT_EVENT_CONNECTED:
            s_connected = true;
            ESP_LOGI(TAG, "connected");
            esp_mqtt_client_publish(s_client, s_status_topic, s_online_msg, 0, 1, true);
            esp_mqtt_client_subscribe(s_client, s_cmd_topic, 1);
            break;
        case MQTT_EVENT_DISCONNECTED:
            s_connected = false;
            ESP_LOGW(TAG, "disconnected");
            break;
        case MQTT_EVENT_DATA:
            if (e->topic_len == (int)strlen(s_cmd_topic) && strncmp(e->topic, s_cmd_topic, e->topic_len) == 0)
                handle_cmd(e->data, e->data_len);
            break;
        default:
            break;
    }
}

void ha_mqtt_start(const char *broker_uri, const char *node_id) {
    snprintf(s_node, sizeof(s_node), "%s", node_id);
    snprintf(s_status_topic, sizeof(s_status_topic), "home/edge/%s/status", s_node);
    snprintf(s_cmd_topic, sizeof(s_cmd_topic), "home/edge/%s/cmd", s_node);
    const esp_partition_t *run = esp_ota_get_running_partition();
    snprintf(s_online_msg, sizeof(s_online_msg), "online %s %s", run ? run->label : "?", HA_FW_VERSION);

    esp_mqtt_client_config_t cfg = {
        .broker.address.uri = broker_uri,
        // Latent broker creds: NULL when empty (anonymous today); used after the auth cutover.
        .credentials.username = HA_MQTT_USER[0] ? HA_MQTT_USER : NULL,
        .credentials.authentication.password = HA_MQTT_PASS[0] ? HA_MQTT_PASS : NULL,
        .session.last_will = { .topic = s_status_topic, .msg = "offline", .msg_len = 0, .qos = 1, .retain = true },
        .session.keepalive = 30,
        .network.reconnect_timeout_ms = 5000,
    };
    s_client = esp_mqtt_client_init(&cfg);
    esp_mqtt_client_register_event(s_client, ESP_EVENT_ANY_ID, on_mqtt, NULL);
    esp_mqtt_client_start(s_client);
}

bool ha_mqtt_is_connected(void) { return s_connected; }

void ha_mqtt_publish_reading(const char *mac_str, const sb_reading_t *r, int rssi) {
    if (!s_connected) return;
    char mf[13]; macflat(mac_str, mf);
    char topic[80];
    snprintf(topic, sizeof(topic), "home/edge/%s/%s/adv", s_node, mf);

    char ts[24];
    if (!ha_sntp_iso_utc(ts, sizeof(ts))) ts[0] = '\0';
    char metrics[96];
    if (r->battery_pct >= 0)
        snprintf(metrics, sizeof(metrics), "{\"temperature_c\":%.1f,\"humidity_pct\":%d,\"battery_pct\":%d}",
                 r->temperature_c, r->humidity_pct, r->battery_pct);
    else
        snprintf(metrics, sizeof(metrics), "{\"temperature_c\":%.1f,\"humidity_pct\":%d}",
                 r->temperature_c, r->humidity_pct);

    char payload[320];
    int n = snprintf(payload, sizeof(payload),
        "{\"schema\":1,\"node\":\"%s\",\"mac\":\"%s\",\"device_type\":\"%s\","
        "\"ts\":\"%s\",\"transport\":\"ble-adv\",\"metrics\":%s,\"meta\":{\"rssi\":%d}}",
        s_node, mac_str, r->device_type, ts, metrics, rssi);
    if (n <= 0 || n >= (int)sizeof(payload)) return;
    esp_mqtt_client_publish(s_client, topic, payload, n, 1, false);
}

void ha_mqtt_publish_history(const char *mac_str, const char *payload) {
    if (!s_connected) return;
    char mf[13]; macflat(mac_str, mf);
    char topic[80];
    snprintf(topic, sizeof(topic), "home/edge/%s/%s/history", s_node, mf);
    esp_mqtt_client_publish(s_client, topic, payload, 0, 1, false);
}

void ha_mqtt_publish_reply(const char *reqid, const char *payload) {
    if (!s_connected) return;
    char topic[80];
    snprintf(topic, sizeof(topic), "home/edge/%s/%s/reply", s_node, reqid);
    esp_mqtt_client_publish(s_client, topic, payload, 0, 1, false);
}

void ha_mqtt_log(const char *fmt, ...) {
    char msg[200];
    va_list ap; va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    ESP_LOGI("ha_edge", "%s", msg);          // also goes to serial when attached
    if (!s_connected) return;
    char topic[64];
    snprintf(topic, sizeof(topic), "home/edge/%s/log", s_node);
    esp_mqtt_client_publish(s_client, topic, msg, 0, 0, false);
}
