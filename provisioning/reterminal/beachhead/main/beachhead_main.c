// D1001 beachhead v5-dbg — permanent, runtime-toggled REMOTE DEBUG over MQTT.
//   Diagnostics stay compiled in forever; default OFF at boot (near-zero cost),
//   flipped on over WiFi when needed. No serial console required.
//
// ALWAYS ON (cheap): retained heartbeat/health, last-will, OTA lifecycle topic.
// GATED by debug (default off): full esp-idf log firehose -> MQTT.
//
// Topics (broker .210):
//   d1001-beachhead/status     <- retained heartbeat {partition,build,ip,uptime,heap,rssi,wifi_rc,mqtt_rc,debug}
//                                 (retained LWT "offline" on unexpected drop)
//   d1001-beachhead/ota        <- OTA lifecycle (begin/progress/complete/fail) — ALWAYS published
//   d1001-beachhead/log        <- full log stream, ONLY when debug on (qos0)
//   d1001-beachhead/ack        <- command receipts
//   d1001-beachhead/cmd/debug  -> "on"/"off" (or 1/0): toggle the log firehose (default off)
//   d1001-beachhead/cmd/ota    -> payload = http URL of the new .bin
//   d1001-beachhead/cmd/ping   -> (any) -> forces an immediate status publish
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdarg.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "mqtt_client.h"
#include "esp_ota_ops.h"
#include "esp_https_ota.h"
#include "esp_http_client.h"
#include "bsp_display.h"
#include "ui_tiles.h"
#include "secrets.h"

#define APP_BUILD_TAG "v17-cmd"
static const char *TAG = "beachhead";

#define T_STATUS "d1001-beachhead/status"
#define T_OTAST  "d1001-beachhead/ota"
#define T_LOG    "d1001-beachhead/log"
#define T_ACK    "d1001-beachhead/ack"
#define T_OTA    "d1001-beachhead/cmd/ota"
#define T_PING   "d1001-beachhead/cmd/ping"
#define T_DEBUG  "d1001-beachhead/cmd/debug"
#define T_DISP   "d1001-beachhead/display"       // <- retained display bring-up result
#define T_DISPC  "d1001-beachhead/cmd/display"    // -> "on" triggers display bring-up (default off)

static esp_mqtt_client_handle_t s_client = NULL;
static volatile bool s_mqtt_up = false;
static volatile bool s_debug = false;         // <-- diagnostic firehose, default OFF
static EventGroupHandle_t s_evt;
#define WIFI_CONNECTED_BIT BIT0
static char s_ip[16] = "?";
static volatile int s_wifi_rc = 0, s_mqtt_rc = 0;
static QueueHandle_t s_log_q = NULL;
static int (*s_orig_vprintf)(const char *, va_list) = NULL;

// Log hook: ALWAYS writes the console; enqueues for MQTT ONLY when debug is on.
static int log_vprintf(const char *fmt, va_list ap)
{
    va_list ap2; va_copy(ap2, ap);
    int r = s_orig_vprintf ? s_orig_vprintf(fmt, ap2) : vprintf(fmt, ap2);
    va_end(ap2);
    if (s_debug && s_log_q) {
        char *buf = malloc(240);
        if (buf) {
            int n = vsnprintf(buf, 240, fmt, ap);
            if (n > 0) {
                size_t l = strlen(buf);
                while (l && (buf[l-1] == '\n' || buf[l-1] == '\r')) buf[--l] = 0;
                if (l == 0 || xQueueSend(s_log_q, &buf, 0) != pdTRUE) free(buf);
            } else free(buf);
        }
    }
    return r;
}

static void log_drain_task(void *pv)
{
    char *line;
    for (;;) {
        if (xQueueReceive(s_log_q, &line, portMAX_DELAY) == pdTRUE) {
            if (s_client && s_mqtt_up && s_debug) esp_mqtt_client_publish(s_client, T_LOG, line, 0, 0, 0);
            free(line);
        }
    }
}

static void ota_report(const char *s)   // OTA lifecycle — always visible, independent of debug
{
    if (s_client && s_mqtt_up) esp_mqtt_client_publish(s_client, T_OTAST, s, 0, 1, 0);
}

static void publish_status(void)
{
    if (!s_client || !s_mqtt_up) return;
    const esp_partition_t *run = esp_ota_get_running_partition();
    wifi_ap_record_t ap; int rssi = 0;
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) rssi = ap.rssi;
    char msg[256];
    snprintf(msg, sizeof(msg),
        "{\"device\":\"d1001-beachhead\",\"status\":\"online\",\"partition\":\"%s\",\"build\":\"%s\","
        "\"ip\":\"%s\",\"uptime_s\":%lld,\"heap\":%u,\"rssi\":%d,\"wifi_rc\":%d,\"mqtt_rc\":%d,"
        "\"display\":%s,\"debug\":%s}",
        run ? run->label : "?", APP_BUILD_TAG, s_ip,
        esp_timer_get_time() / 1000000, (unsigned)esp_get_free_heap_size(), rssi, s_wifi_rc, s_mqtt_rc,
        bsp_display_ready() ? "true" : "false", s_debug ? "true" : "false");
    esp_mqtt_client_publish(s_client, T_STATUS, msg, 0, 1, 1);   // qos1 retained
}

static void heartbeat_task(void *pv)
{
    for (;;) { publish_status(); vTaskDelay(pdMS_TO_TICKS(15000)); }
}

// Bring up the panel on demand (triggered by cmd/display "on"), NOT at boot, so
// the net + OTA lifeline is always established first and a failed bring-up can
// only cost a reboot back into this same good firmware — never a brick. Non-fatal.
static volatile bool s_disp_started = false;
static void display_task(void *pv)
{
    if (s_disp_started) {   // idempotent: re-trigger just republishes state
        if (s_client && s_mqtt_up)
            esp_mqtt_client_publish(s_client, T_DISP,
                bsp_display_ready() ? "{\"display\":\"online\",\"note\":\"already up\"}"
                                    : "{\"display\":\"failed\",\"note\":\"already attempted\"}", 0, 1, 1);
        vTaskDelete(NULL); return;
    }
    s_disp_started = true;
    ESP_LOGW(TAG, "display bring-up requested — starting");
    esp_err_t err = bsp_display_start();
    char m[128];
    if (err == ESP_OK) {
        snprintf(m, sizeof(m), "{\"display\":\"online\",\"panel\":\"jd9365\",\"res\":\"800x1280\",\"build\":\"%s\"}", APP_BUILD_TAG);
        ESP_LOGW(TAG, ">>> DISPLAY ONLINE <<<");
        ui_tiles_start(BFF_BASE_URL "/api/v1/sensors");   // server-backed tiles from the BFF
    } else {
        snprintf(m, sizeof(m), "{\"display\":\"failed\",\"err\":\"%s\",\"build\":\"%s\"}", esp_err_to_name(err), APP_BUILD_TAG);
        ESP_LOGE(TAG, "display init failed: %s (device stays live on the bus)", esp_err_to_name(err));
    }
    if (s_client && s_mqtt_up) esp_mqtt_client_publish(s_client, T_DISP, m, 0, 1, 1);  // retained
    vTaskDelete(NULL);
}

static void ota_task(void *pv)
{
    char *url = (char *)pv;
    ESP_LOGW(TAG, "OTA: begin url=%s", url);
    ota_report("{\"ota\":\"begin\"}");
    esp_http_client_config_t http = { .url = url, .timeout_ms = 30000, .keep_alive_enable = true };
    esp_https_ota_config_t cfg = { .http_config = &http };
    esp_https_ota_handle_t h = NULL;
    esp_err_t err = esp_https_ota_begin(&cfg, &h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OTA: begin FAILED: %s", esp_err_to_name(err));
        char m[96]; snprintf(m, sizeof(m), "{\"ota\":\"begin_failed\",\"err\":\"%s\"}", esp_err_to_name(err));
        ota_report(m); free(url); vTaskDelete(NULL); return;
    }
    ota_report("{\"ota\":\"connected\"}");
    int last = 0;
    while (1) {
        err = esp_https_ota_perform(h);
        if (err != ESP_ERR_HTTPS_OTA_IN_PROGRESS) break;
        int n = esp_https_ota_get_image_len_read(h);
        if (n - last >= 131072) {
            char m[64]; snprintf(m, sizeof(m), "{\"ota\":\"progress\",\"bytes\":%d}", n);
            ota_report(m); last = n;
        }
    }
    if (err == ESP_OK && esp_https_ota_is_complete_data_received(h) && esp_https_ota_finish(h) == ESP_OK) {
        ESP_LOGW(TAG, ">>> OTA COMPLETE — rebooting <<<");
        ota_report("{\"ota\":\"complete\",\"action\":\"rebooting\"}");
        bsp_display_off();               // dark the panel before reset (no flash/white during reboot)
        vTaskDelay(pdMS_TO_TICKS(800));
        esp_restart();
    } else {
        ESP_LOGE(TAG, "OTA: FAILED: %s", esp_err_to_name(err));
        char m[96]; snprintf(m, sizeof(m), "{\"ota\":\"failed\",\"err\":\"%s\"}", esp_err_to_name(err));
        ota_report(m);
        esp_https_ota_abort(h);
    }
    free(url);
    vTaskDelete(NULL);
}

static void mqtt_event_handler(void *args, esp_event_base_t base, int32_t id, void *data)
{
    esp_mqtt_event_handle_t e = (esp_mqtt_event_handle_t)data;
    switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_CONNECTED:
        s_mqtt_up = true;
        esp_mqtt_client_subscribe(e->client, "d1001-beachhead/cmd/#", 1);
        esp_mqtt_client_subscribe(e->client, "home/+/+/state", 0);   // live device state -> tiles
        ESP_LOGW(TAG, "MQTT connected (reconnect #%d) — subscribed cmd/# + home/+/+/state", s_mqtt_rc);
        publish_status();
        esp_ota_mark_app_valid_cancel_rollback();
        break;
    case MQTT_EVENT_DISCONNECTED:
        s_mqtt_up = false; s_mqtt_rc++;
        break;
    case MQTT_EVENT_DATA: {
        int tl = e->topic_len, dl = e->data_len;
        // Live device state (high volume) -> UI. No ack/echo, no per-message log.
        if (tl > 5 && strncmp(e->topic, "home/", 5) == 0) {
            char *p = strndup(e->data, dl);
            if (p) { ui_tiles_on_state(p); free(p); }
            break;
        }
        ESP_LOGW(TAG, "MQTT DATA topic=%.*s payload=%.*s", tl, e->topic, dl, e->data);
        esp_mqtt_client_publish(e->client, T_ACK, e->topic, tl, 0, 0);
        if (tl == (int)strlen(T_OTA) && strncmp(e->topic, T_OTA, tl) == 0) {
            char *url = strndup(e->data, dl);
            if (url) xTaskCreate(ota_task, "ota", 8192, url, 5, NULL);
        } else if (tl == (int)strlen(T_PING) && strncmp(e->topic, T_PING, tl) == 0) {
            publish_status();
        } else if (tl == (int)strlen(T_DISPC) && strncmp(e->topic, T_DISPC, tl) == 0) {
            bool on = (dl >= 1 && (e->data[0] == '1' || e->data[0] == 'o' || e->data[0] == 'O' ||
                                   e->data[0] == 't' || e->data[0] == 'T'));
            if (on) xTaskCreate(display_task, "disp", 8192, NULL, 4, NULL);   // bring-up on demand
        } else if (tl == (int)strlen(T_DEBUG) && strncmp(e->topic, T_DEBUG, tl) == 0) {
            s_debug = (dl >= 1 && (e->data[0] == '1' || e->data[0] == 'o' || e->data[0] == 'O' ||
                                   e->data[0] == 't' || e->data[0] == 'T'));   // on/1/true
            ESP_LOGW(TAG, "debug firehose -> %s", s_debug ? "ON" : "OFF");
            publish_status();
        }
        break;
    }
    case MQTT_EVENT_ERROR: ESP_LOGE(TAG, "MQTT error"); break;
    default: break;
    }
}

static void start_mqtt(void)
{
    esp_mqtt_client_config_t cfg = {
        .broker.address.uri = MQTT_BROKER_URI,
        .credentials.client_id = "d1001-beachhead",
        .session.keepalive = 15,
        .session.last_will = {
            .topic = T_STATUS,
            .msg = "{\"device\":\"d1001-beachhead\",\"status\":\"offline\"}",
            .qos = 1, .retain = 1,
        },
    };
    s_client = esp_mqtt_client_init(&cfg);
    esp_mqtt_client_register_event(s_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    esp_mqtt_client_start(s_client);
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *d = (wifi_event_sta_disconnected_t *)data;
        s_wifi_rc++;
        ESP_LOGW(TAG, "WiFi DISCONNECTED reason=%d (reconnect #%d)", d ? d->reason : -1, s_wifi_rc);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *evt = (ip_event_got_ip_t *)data;
        snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&evt->ip_info.ip));
        ESP_LOGI(TAG, "GOT IP: %s", s_ip);
        xEventGroupSetBits(s_evt, WIFI_CONNECTED_BIT);
    }
}

void app_main(void)
{
    s_log_q = xQueueCreate(48, sizeof(char *));
    xTaskCreate(log_drain_task, "logdrain", 4096, NULL, 4, NULL);
    s_orig_vprintf = esp_log_set_vprintf(log_vprintf);   // permanent; gated by s_debug
    esp_log_level_set("mqtt_client", ESP_LOG_WARN);      // avoid log->publish->log storms when debug on
    esp_log_level_set("transport", ESP_LOG_WARN);
    esp_log_level_set("transport_base", ESP_LOG_WARN);
    esp_log_level_set("esp-tls", ESP_LOG_WARN);
    esp_log_level_set("outbox", ESP_LOG_WARN);

    ESP_LOGW(TAG, "=== D1001 beachhead %s (remote-debug over MQTT; debug OFF) ===", APP_BUILD_TAG);

    esp_err_t r = nvs_flash_init();
    if (r == ESP_ERR_NVS_NO_FREE_PAGES || r == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    s_evt = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        wifi_event_handler, NULL, NULL));

    wifi_config_t wc = { 0 };
    strncpy((char *)wc.sta.ssid, WIFI_SSID, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, WIFI_PASS, sizeof(wc.sta.password) - 1);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "WiFi started — joining %s", WIFI_SSID);

    xEventGroupWaitBits(s_evt, WIFI_CONNECTED_BIT, pdFALSE, pdTRUE, portMAX_DELAY);
    ESP_LOGI(TAG, "WiFi up — starting MQTT");
    start_mqtt();
    xTaskCreate(heartbeat_task, "hb", 4096, NULL, 3, NULL);
    // Display is NOT started here — trigger it over MQTT with cmd/display "on"
    // once the device is confirmed live, so a failed bring-up can't brick boot.
}
