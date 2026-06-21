#include "ha_mqtt.h"
#include "ha_sntp.h"
#include <stdio.h>
#include <string.h>
#include "mqtt_client.h"
#include "esp_log.h"

static const char *TAG = "ha_mqtt";
static esp_mqtt_client_handle_t s_client;
static char s_node[32];
static char s_status_topic[64];
static volatile bool s_connected;

static void on_mqtt(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data) {
    esp_mqtt_event_handle_t e = event_data;
    switch ((esp_mqtt_event_id_t)event_id) {
        case MQTT_EVENT_CONNECTED:
            s_connected = true;
            ESP_LOGI(TAG, "connected");
            esp_mqtt_client_publish(s_client, s_status_topic, "online", 0, 1, true);
            break;
        case MQTT_EVENT_DISCONNECTED:
            s_connected = false;
            ESP_LOGW(TAG, "disconnected");
            break;
        default:
            (void)e;
            break;
    }
}

void ha_mqtt_start(const char *broker_uri, const char *node_id) {
    snprintf(s_node, sizeof(s_node), "%s", node_id);
    snprintf(s_status_topic, sizeof(s_status_topic), "home/edge/%s/status", s_node);

    esp_mqtt_client_config_t cfg = {
        .broker.address.uri = broker_uri,
        .session.last_will = {
            .topic = s_status_topic,
            .msg = "offline",
            .msg_len = 0,
            .qos = 1,
            .retain = true,
        },
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

    // topic: home/edge/<node>/<mac-lower-no-colons>/adv
    char macflat[13]; int j = 0;
    for (const char *p = mac_str; *p && j < 12; ++p) {
        if (*p == ':') continue;
        char c = *p;
        macflat[j++] = (c >= 'A' && c <= 'Z') ? (c + 32) : c;
    }
    macflat[j] = '\0';
    char topic[80];
    snprintf(topic, sizeof(topic), "home/edge/%s/%s/adv", s_node, macflat);

    char ts[24];
    if (!ha_sntp_iso_utc(ts, sizeof(ts))) ts[0] = '\0';   // mapper stamps if empty

    // metrics object — battery optional
    char metrics[96];
    if (r->battery_pct >= 0) {
        snprintf(metrics, sizeof(metrics),
                 "{\"temperature_c\":%.1f,\"humidity_pct\":%d,\"battery_pct\":%d}",
                 r->temperature_c, r->humidity_pct, r->battery_pct);
    } else {
        snprintf(metrics, sizeof(metrics),
                 "{\"temperature_c\":%.1f,\"humidity_pct\":%d}",
                 r->temperature_c, r->humidity_pct);
    }

    char payload[320];
    int n = snprintf(payload, sizeof(payload),
        "{\"schema\":1,\"node\":\"%s\",\"mac\":\"%s\",\"device_type\":\"%s\","
        "\"ts\":\"%s\",\"transport\":\"ble-adv\",\"metrics\":%s,\"meta\":{\"rssi\":%d}}",
        s_node, mac_str, r->device_type, ts, metrics, rssi);
    if (n <= 0 || n >= (int)sizeof(payload)) return;

    esp_mqtt_client_publish(s_client, topic, payload, n, 1, false);
    ESP_LOGD(TAG, "pub %s %s", topic, payload);
}
