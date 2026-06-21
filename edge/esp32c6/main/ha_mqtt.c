#include "ha_mqtt.h"
#include "ha_sntp.h"
#include "gatt_history.h"
#include "gatt_exec.h"
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include "mqtt_client.h"
#include "esp_log.h"
#include "cJSON.h"

static const char *TAG = "ha_mqtt";
static esp_mqtt_client_handle_t s_client;
static char s_node[32];
static char s_status_topic[64];
static char s_cmd_topic[64];
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

static void handle_cmd(const char *data, int len) {
    cJSON *root = cJSON_ParseWithLength(data, len);
    if (!root) { ESP_LOGW(TAG, "bad cmd json"); return; }
    const cJSON *op = cJSON_GetObjectItem(root, "op");
    const cJSON *mac = cJSON_GetObjectItem(root, "mac");
    const cJSON *prof = cJSON_GetObjectItem(root, "profile");
    if (cJSON_IsString(op) && strcmp(op->valuestring, "history") == 0 && cJSON_IsString(mac)) {
        const char *profile = cJSON_IsString(prof) ? prof->valuestring : "outdoor";
        ESP_LOGI(TAG, "cmd: history pull mac=%s profile=%s", mac->valuestring, profile);
        if (gatt_exec_busy()) ESP_LOGW(TAG, "central busy; dropping history pull");
        else gatt_history_pull(mac->valuestring, profile);
    } else if (cJSON_IsString(op) && strcmp(op->valuestring, "gatt") == 0 && cJSON_IsString(mac)) {
        // Generic GATT forwarder: {"op":"gatt","reqid":"..","mac":"..","steps":[...]}
        const cJSON *reqid = cJSON_GetObjectItem(root, "reqid");
        const cJSON *steps = cJSON_GetObjectItem(root, "steps");
        if (!cJSON_IsArray(steps)) { ESP_LOGW(TAG, "gatt cmd: missing steps[]"); cJSON_Delete(root); return; }
        char *steps_json = cJSON_PrintUnformatted(steps);   // re-serialise just the steps array
        const char *rid = cJSON_IsString(reqid) ? reqid->valuestring : "0";
        ESP_LOGI(TAG, "cmd: gatt exec mac=%s reqid=%s", mac->valuestring, rid);
        if (gatt_history_busy() || gatt_exec_busy()) ESP_LOGW(TAG, "central busy; dropping gatt exec");
        else if (steps_json) gatt_exec_run(rid, mac->valuestring, steps_json);
        if (steps_json) cJSON_free(steps_json);
    } else {
        ESP_LOGW(TAG, "unknown/!malformed cmd");
    }
    cJSON_Delete(root);
}

static void on_mqtt(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data) {
    esp_mqtt_event_handle_t e = event_data;
    switch ((esp_mqtt_event_id_t)event_id) {
        case MQTT_EVENT_CONNECTED:
            s_connected = true;
            ESP_LOGI(TAG, "connected");
            esp_mqtt_client_publish(s_client, s_status_topic, "online", 0, 1, true);
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

    esp_mqtt_client_config_t cfg = {
        .broker.address.uri = broker_uri,
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
