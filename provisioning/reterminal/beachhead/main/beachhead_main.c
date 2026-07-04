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
#include "driver/gpio.h"
#include "bsp_display.h"
#include "esp_io_expander.h"
#include "bat_profile.h"
#include "ha_battery.h"
#include "ha_imu.h"                 // roadmap #3 (ability A): IMU presence + tap-to-wake
#include "ha_battery_profile_rt.h"   // runtime profile load/save/push — deploy a curve as data (ADR-0024 §5)
#include "ha_power_policy.h"   // battery safety policy: shutdown/warn/boot-gate (ADR-0024)
#include "fs_ops.h"
#include "ui_tiles.h"
#include "ha_replica.h"   // ADR-0022 Phase 1a: rung DB replica to SD
#include "ha_sdcard.h"    // SD mount + hot-plug watcher (card-detect GPIO45)
#include "ha_ble_scan.h"
#include "ha_reach.h"
#include "esp_hosted.h"
#include "esp_hosted_ota.h"
#include "secrets.h"
#include <time.h>
#include <sys/time.h>
#include "esp_netif_sntp.h"
#include "esp_sntp.h"
#include "ha_rtc.h"                 // roadmap #1 (ability G): PCF8563 wall clock

#ifndef NTP_SERVER
#define NTP_SERVER "192.168.0.210"  // dictator LAN IP — serves NTP via chrony (unblocks the wall clock)
#endif
#ifndef PANEL_TZ
#define PANEL_TZ "PST8PDT,M3.2.0,M11.1.0"   // America/Los_Angeles (POSIX TZ); override in secrets.h
#endif

#define APP_BUILD_TAG "v59-clock-presence"
// Edge-node identity for BLE advert relay. The panel is a peer edge node (ADR-0020):
// decoded meters publish to home/edge/<BLE_NODE>/<mac>/adv, same shape the c3/c6/s3
// nodes emit, so the dictator's edge-mapper ingests it with zero new server work.
#define BLE_NODE "d1001-beachhead"
#define BSP_BUTTON_IN  GPIO_NUM_3     // back-of-device button, active-low w/ pull-up
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
#define T_BLEC   "d1001-beachhead/cmd/ble"        // -> "on" starts the passive BLE relay (default off)
#define T_BLE    "d1001-beachhead/ble"            // <- BLE relay telemetry (adv total / decoded / rssi)
#define T_SLVOTA "d1001-beachhead/cmd/slaveota"   // -> URL of C6 slave app bin (network_adapter.bin)
#define T_SLVST  "d1001-beachhead/slaveota"       // <- C6 slave-OTA result
#define T_I2CSC  "d1001-beachhead/cmd/i2cscan"    // -> (any) probe both I2C buses (find fuel gauge)
#define T_I2CRES "d1001-beachhead/i2c"            // <- I2C scan result
#define T_BDUMP  "d1001-beachhead/cmd/battdump"   // -> (any) dump 0x36 MAX17048 regs (chip ID)
#define T_BPROF  "d1001-beachhead/battprofile"    // <- live mirror of the SD battery-profile rows
#define T_FSC    "d1001-beachhead/cmd/fs"          // -> JSON SD file op (ls/stat/read/write/rm/mkdir/df)
#define T_FS     "d1001-beachhead/fs"              // <- JSON file-op result
#define T_PWR    "d1001-beachhead/power"           // <- power-context change (on_wall / ble_relay), retained
#define T_SCRC   "d1001-beachhead/cmd/screen"      // -> off/on/toggle (or 0/1): control backlight+panel power
#define T_GPIOC  "d1001-beachhead/cmd/gpio"        // -> "N" read P4 GPIO N | "N 0|1" drive it. Result -> pin
#define T_EXPC   "d1001-beachhead/cmd/exp"         // -> "N" read PCA9535 pin N | "N 0|1" drive it. Result -> pin
#define T_PIN    "d1001-beachhead/pin"             // <- {gpio|exp, level|set} readback for cmd/gpio + cmd/exp
#define T_CHGC   "d1001-beachhead/cmd/charge"      // -> auto|hold|on|off | reset[ ms] | status : charge-mgr control
#define T_CHG    "d1001-beachhead/charge"          // <- charge-manager state (mode + STAT + cell)
#define T_ALERT  "d1001-beachhead/alert"           // <- low-battery warn (retained) -> ntfy bridge
#define T_BRATE  "d1001-beachhead/cmd/battrate"    // -> N: battery telemetry sample period in ms (finer-cadence hook)
#define T_PPTEST "d1001-beachhead/cmd/pptest"      // -> "mv N": inject a policy reading to bench-test warn/shutdown (0=resume)
#define T_PROFC  "d1001-beachhead/cmd/profile"     // -> JSON profile (hot-swap+persist to NVS) | "get" | "default" (ADR-0024 §5)
#define T_PROF   "d1001-beachhead/profile"         // <- active battery profile (provenance+offsets+lut+source), retained

static esp_mqtt_client_handle_t s_client = NULL;
static volatile bool s_mqtt_up = false;
static void ble_task(void *pv);         // passive BLE relay task (defined near start_mqtt)
static void ble_ensure_started(void);   // idempotent one-shot BLE bring-up (power-aware + cmd/ble)
static void power_task(void *pv);       // power-context watcher: BLE on wall / off battery + notify
static void slave_ota_task(void *pv);   // C6 slave-OTA task (defined near start_mqtt)
static void i2cscan_task(void *pv);     // I2C bus scan (fuel-gauge ID) — defined near start_mqtt
static void battdump_task(void *pv);    // 0x36 register dump (chip ID) — defined near start_mqtt
static void profile_apply_task(void *pv);   // apply/persist a pushed battery profile (JSON) off the mqtt stack
static void publish_profile(void);          // retained /profile status of the active curve
static char s_profile_source[12] = "default";   // how the active curve was set: "nvs" | "default" | "pushed"
static volatile bool s_batt_ready = false;       // ha_battery_init done — safe to publish /profile
static volatile bool s_debug = false;         // <-- diagnostic firehose, default OFF

// SD hot-plug: the watcher mounts/unmounts; we drive the replica cache off the presence change —
// insert re-inventories the local rung DB, removal drops it (queries fall back to the network).
static void sd_presence_changed(bool present)
{
    if (present) ha_replica_sd_inserted();
    else         ha_replica_sd_removed();
}
// Power-aware BLE state (defined here so the cmd/ble handler can force-on; logic below).
static volatile bool s_ble_started  = false;  // ha_ble_scan_start done once (task created)
static volatile bool s_on_wall      = false;  // last observed wall-power state (starts battery)
static volatile bool s_ble_relaying = false;  // scan currently active (started & not paused)
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
    ha_batt_sample_t bs = {0};
    bool have_batt = (ha_battery_sample(&bs) == ESP_OK);
    if (have_batt && bsp_display_ready())              // only touch LVGL once it's initialized
        ui_tiles_set_battery(bs.soc, bs.on_wall, bs.gaining);   // panel top-bar power indicator (3-state)
    char msg[380];
    snprintf(msg, sizeof(msg),
        "{\"device\":\"d1001-beachhead\",\"status\":\"online\",\"partition\":\"%s\",\"build\":\"%s\","
        "\"ip\":\"%s\",\"uptime_s\":%lld,\"heap\":%u,\"rssi\":%d,\"wifi_rc\":%d,\"mqtt_rc\":%d,"
        "\"display\":%s,\"debug\":%s,\"batt_pct\":%d,\"batt_mv\":%d,\"charging\":%s,"
        "\"on_wall\":%s,\"gaining\":%s,\"temp_dc\":%d,\"charge_en\":%s}",
        run ? run->label : "?", APP_BUILD_TAG, s_ip,
        esp_timer_get_time() / 1000000, (unsigned)esp_get_free_heap_size(), rssi, s_wifi_rc, s_mqtt_rc,
        bsp_display_ready() ? "true" : "false", s_debug ? "true" : "false",
        have_batt ? bs.soc : -1, have_batt ? bs.batt_mv_smoothed : 0, bs.charging ? "true" : "false",
        bs.on_wall ? "true" : "false", bs.gaining ? "true" : "false",
        bs.have_temp ? bs.temp_dc : -9999, bs.charge_en ? "true" : "false");
    esp_mqtt_client_publish(s_client, T_STATUS, msg, 0, 1, 1);   // qos1 retained
}

static void heartbeat_task(void *pv)
{
    for (;;) { publish_status(); vTaskDelay(pdMS_TO_TICKS(15000)); }
}

// Back button (GPIO3, active-low): short-press toggles the screen on/off. A tiny
// debounced poll task — no button component (zero deps, no API-version churn).
// Toggle is a no-op until the display has been brought up (cmd/display on).
static void button_task(void *pv)
{
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << BSP_BUTTON_IN,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);
    int prev = 1;                       // released (pull-up high)
    for (;;) {
        int lvl = gpio_get_level(BSP_BUTTON_IN);
        if (prev == 1 && lvl == 0) {    // high->low = press edge
            vTaskDelay(pdMS_TO_TICKS(30));                 // debounce settle
            if (gpio_get_level(BSP_BUTTON_IN) == 0) {      // still down = real press
                if (bsp_display_ready()) {
                    bsp_display_toggle();
                    if (s_client && s_mqtt_up)
                        esp_mqtt_client_publish(s_client, T_ACK,
                            bsp_display_is_on() ? "button:screen-on" : "button:screen-off", 0, 0, 0);
                }
                // wait for release so a held button = one toggle
                while (gpio_get_level(BSP_BUTTON_IN) == 0) vTaskDelay(pdMS_TO_TICKS(20));
            }
        }
        prev = lvl;
        vTaskDelay(pdMS_TO_TICKS(30));
    }
}

// Bring up the panel on demand (triggered by cmd/display "on"), NOT at boot, so
// the net + OTA lifeline is always established first and a failed bring-up can
// only cost a reboot back into this same good firmware — never a brick. Non-fatal.
static volatile bool s_disp_started = false;
static volatile bool s_auto_disp_done = false;   // one-shot: auto-boot the GUI on first MQTT connect
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
        ha_replica_start(BFF_BASE_URL);                   // ADR-0022 Phase 1a: mirror the rung DB to SD
        // Hot-plug: auto-mount + inventory on insert, drop cache on removal. GPIO45 = SD_DETECT,
        // active-low (card present pulls it to GND; boot level is logged to confirm polarity).
        ha_sdcard_watch(NULL, 45, true, sd_presence_changed);
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
    // Blank the panel for the whole download. Flash writes momentarily disable the cache, stalling the
    // PSRAM-resident MIPI-DSI framebuffer fetch -> the panel STROBES while it's lit (Hugh, 2026-07-01:
    // the flash is during flash-write, not reboot). sleep() keeps the panel powered (no re-init), just
    // backlight+display off; wake() restores it if the OTA fails.
    bsp_display_sleep();
    esp_http_client_config_t http = { .url = url, .timeout_ms = 30000, .keep_alive_enable = true };
    esp_https_ota_config_t cfg = { .http_config = &http };
    esp_https_ota_handle_t h = NULL;
    esp_err_t err = esp_https_ota_begin(&cfg, &h);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OTA: begin FAILED: %s", esp_err_to_name(err));
        char m[96]; snprintf(m, sizeof(m), "{\"ota\":\"begin_failed\",\"err\":\"%s\"}", esp_err_to_name(err));
        ota_report(m); bsp_display_wake(); free(url); vTaskDelete(NULL); return;
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
        bsp_display_wake();              // restore the panel (no reboot on failure)
    }
    free(url);
    vTaskDelete(NULL);
}

// --- Battery safety policy (ADR-0024): shutdown / warn / boot-gate. Decisions live in the shared
// ha_power_policy component; these are the D1001 actuators injected into it. ---
#define BSP_LED_R  GPIO_NUM_22           // red status LED, active-low (Seeed direct P4 GPIO)
static ha_power_policy_cfg_t s_pp_cfg;   // set early in app_main (ha_power_policy_d1001_cfg)
static volatile int s_pp_test_mv = 0;    // >0 overrides the policy reading — bench test of warn/shutdown

static void led_init(void) { gpio_reset_pin(BSP_LED_R); gpio_set_direction(BSP_LED_R, GPIO_MODE_OUTPUT); gpio_set_level(BSP_LED_R, 1); }
static void pp_led(void *ctx, bool on)  { gpio_set_level(BSP_LED_R, on ? 0 : 1); }   // active-low
static void pp_power_off(void *ctx)     { bsp_power_off(); }
static void pp_spawn_display(void *ctx) { xTaskCreate(display_task, "disp", 8192, NULL, 4, NULL); }
static void pp_warn(void *ctx, bool on)
{
    if (s_client && s_mqtt_up)
        esp_mqtt_client_publish(s_client, T_ALERT,
            on ? "{\"battery\":\"low\",\"msg\":\"charge the panel\"}" : "{\"battery\":\"ok\"}", 0, 1, 1);  // retained -> ntfy
}
static int pp_read_mv(void *ctx)
{
    if (s_pp_test_mv > 0) return s_pp_test_mv;         // bench override (test warn/shutdown without draining)
    ha_batt_sample_t s = {0};
    if (ha_battery_sample(&s) != ESP_OK) return 9999;  // gauge glitch -> don't trip (fail-safe high)
    if (s.on_wall) return 9999;                        // on external power -> no over-discharge risk
    return s.batt_mv_norm;                             // on battery: the normalized cell drives the policy
}
static ha_power_policy_io_t s_pp_io = {
    .read_mv = pp_read_mv, .power_off = pp_power_off, .warn = pp_warn, .led = pp_led, .ctx = NULL,
};

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
        if (s_batt_ready) publish_profile();   // re-assert the retained active-profile status on (re)connect
        esp_ota_mark_app_valid_cancel_rollback();
        // Auto-boot the GUI now that the net + OTA lifeline is CONFIRMED live (marked valid above).
        // The panel is reliable enough (Hugh, 2026-07-01) and predark held it dark since boot, so there
        // is no strobe. Rollback safety is preserved: a firmware that can't reach the broker never gets
        // here, so it never auto-brings-up — it just reboots/rolls back. cmd/display on stays as a manual
        // retry. Idempotent (display_task guards on s_disp_started; the flag stops reconnect re-triggers).
        if (!s_auto_disp_done) {
            s_auto_disp_done = true;
            // Boot-gate the display (ADR-0024 §6): below the cold-start floor, hold the panel dark +
            // blink the red LED, and bring it up only once the cell recovers. Non-blocking — the
            // net/MQTT lifeline stays live while dark; on a gauge glitch it fails toward display-up.
            ha_power_policy_boot_gate(&s_pp_cfg, &s_pp_io, pp_spawn_display);
        }
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
        } else if (tl == (int)strlen(T_BLEC) && strncmp(e->topic, T_BLEC, tl) == 0) {
            bool on = (dl >= 1 && (e->data[0] == '1' || e->data[0] == 'o' || e->data[0] == 'O' ||
                                   e->data[0] == 't' || e->data[0] == 'T'));
            if (on) { ble_ensure_started(); ha_ble_scan_resume(); s_ble_relaying = true; }  // manual force-on (even on battery)
        } else if (tl == (int)strlen(T_SLVOTA) && strncmp(e->topic, T_SLVOTA, tl) == 0) {
            char *url = strndup(e->data, dl);
            if (url) xTaskCreate(slave_ota_task, "slaveota", 8192, url, 5, NULL);   // C6 reflash
        } else if (tl == (int)strlen(T_I2CSC) && strncmp(e->topic, T_I2CSC, tl) == 0) {
            xTaskCreate(i2cscan_task, "i2cscan", 4096, NULL, 4, NULL);   // find fuel gauge
        } else if (tl == (int)strlen(T_BDUMP) && strncmp(e->topic, T_BDUMP, tl) == 0) {
            xTaskCreate(battdump_task, "battdump", 4096, NULL, 4, NULL);   // 0x36 reg dump
        } else if (tl == (int)strlen(T_SCRC) && strncmp(e->topic, T_SCRC, tl) == 0) {
            // screen power: off/on/toggle (or 0/1). Remote equivalent of the back button, so the
            // panel can be darkened for debug/charge tests without physical access. No-op until display up.
            if (bsp_display_ready()) {
                char c0 = dl >= 1 ? e->data[0] : 't';
                char c1 = dl >= 2 ? e->data[1] : 0;
                if (c0 == 't' || c0 == 'T') bsp_display_toggle();                                    // toggle
                else if ((c0=='o'||c0=='O') && (c1=='f'||c1=='F')) { if (bsp_display_is_on()) bsp_display_toggle(); }  // off
                else if ((c0=='o'||c0=='O') && (c1=='n'||c1=='N')) { if (!bsp_display_is_on()) bsp_display_toggle(); } // on
                else if (c0 == '0') { if (bsp_display_is_on()) bsp_display_toggle(); }
                else if (c0 == '1') { if (!bsp_display_is_on()) bsp_display_toggle(); }
                esp_mqtt_client_publish(e->client, T_ACK, bsp_display_is_on() ? "screen:on" : "screen:off", 0, 0, 0);
            }
        } else if (tl == (int)strlen(T_GPIOC) && strncmp(e->topic, T_GPIOC, tl) == 0) {
            char b[24]; int nb = dl < 23 ? dl : 23; memcpy(b, e->data, nb); b[nb] = 0;
            int pin = -1, val = -1; sscanf(b, "%d %d", &pin, &val);   // "N" read | "N 0|1" write
            if (pin >= 0) {
                char out[64];
                if (val == 0 || val == 1) {
                    gpio_set_direction(pin, GPIO_MODE_OUTPUT); gpio_set_level(pin, val);
                    snprintf(out, sizeof(out), "{\"gpio\":%d,\"set\":%d}", pin, val);
                } else snprintf(out, sizeof(out), "{\"gpio\":%d,\"level\":%d}", pin, gpio_get_level(pin));
                esp_mqtt_client_publish(e->client, T_PIN, out, 0, 0, 0);
            }
        } else if (tl == (int)strlen(T_EXPC) && strncmp(e->topic, T_EXPC, tl) == 0) {
            esp_io_expander_handle_t exp = bsp_io_expander();
            char b[24]; int nb = dl < 23 ? dl : 23; memcpy(b, e->data, nb); b[nb] = 0;
            int pin = -1, val = -1; sscanf(b, "%d %d", &pin, &val);   // "N" read | "N 0|1" write (PCA9535 bit)
            if (exp && pin >= 0 && pin < 16) {
                uint32_t mask = 1u << pin; char out[64];
                if (val == 0 || val == 1) {
                    esp_io_expander_set_dir(exp, mask, IO_EXPANDER_OUTPUT);
                    esp_io_expander_set_level(exp, mask, val);
                    snprintf(out, sizeof(out), "{\"exp\":%d,\"set\":%d}", pin, val);
                } else {
                    uint32_t lv = 0; esp_io_expander_get_level(exp, mask, &lv);
                    snprintf(out, sizeof(out), "{\"exp\":%d,\"level\":%d}", pin, (lv & mask) ? 1 : 0);
                }
                esp_mqtt_client_publish(e->client, T_PIN, out, 0, 0, 0);
            }
        } else if (tl == (int)strlen(T_CHGC) && strncmp(e->topic, T_CHGC, tl) == 0) {
            char b[24]; int nb = dl < 23 ? dl : 23; memcpy(b, e->data, nb); b[nb] = 0;
            if      (!strncmp(b, "auto", 4)) ha_battery_charge_mode(HA_CHG_AUTO);
            else if (!strncmp(b, "hold", 4)) ha_battery_charge_mode(HA_CHG_HOLD);
            else if (!strncmp(b, "on",   2)) ha_battery_charge_mode(HA_CHG_ON);
            else if (!strncmp(b, "off",  3)) ha_battery_charge_mode(HA_CHG_OFF);
            else if (!strncmp(b, "reset",5)) { int ms = 0; sscanf(b + 5, "%d", &ms); ha_battery_charge_reset_pulse(ms > 0 ? ms : 500); }
            // publish charge-manager state (mode + live STAT + cell) for exploration
            ha_batt_sample_t cs = {0}; bool okc = (ha_battery_sample(&cs) == ESP_OK);
            const char *mn[] = {"auto","hold","on","off"};
            char out[160];
            snprintf(out, sizeof(out),
                "{\"mode\":\"%s\",\"charging\":%s,\"gaining\":%s,\"batt_mv\":%d,\"soc\":%d,\"charge_en\":%s}",
                mn[ha_battery_charge_mode_get() & 3], cs.charging ? "true" : "false",
                cs.gaining ? "true" : "false", okc ? cs.batt_mv_smoothed : -1, okc ? cs.soc : -1,
                cs.charge_en ? "true" : "false");
            esp_mqtt_client_publish(e->client, T_CHG, out, 0, 0, 0);
        } else if (tl == (int)strlen(T_BRATE) && strncmp(e->topic, T_BRATE, tl) == 0) {
            char b[16]; int nb = dl < 15 ? dl : 15; memcpy(b, e->data, nb); b[nb] = 0;
            int ms = atoi(b);
            int applied = bat_profile_set_rate(ms);   // clamps to a sane range; returns the value used
            char out[48]; snprintf(out, sizeof(out), "{\"battrate_ms\":%d}", applied);
            esp_mqtt_client_publish(e->client, T_ACK, out, 0, 0, 0);
        } else if (tl == (int)strlen(T_PPTEST) && strncmp(e->topic, T_PPTEST, tl) == 0) {
            char b[24]; int nb = dl < 23 ? dl : 23; memcpy(b, e->data, nb); b[nb] = 0;
            int mv = 0;
            if (sscanf(b, "mv %d", &mv) == 1) s_pp_test_mv = mv;   // 0 = resume the real reading
            char out[64]; snprintf(out, sizeof(out), "{\"pptest_mv\":%d,\"note\":\"%s\"}",
                                   s_pp_test_mv, s_pp_test_mv ? "policy sees this reading" : "real reading");
            esp_mqtt_client_publish(e->client, T_ACK, out, 0, 0, 0);
        } else if (tl == (int)strlen(T_PROFC) && strncmp(e->topic, T_PROFC, tl) == 0) {
            char *j = strndup(e->data, dl);   // JSON / "get" / "default" — apply task frees it
            if (j) xTaskCreate(profile_apply_task, "profapply", 6144, j, 4, NULL);
        } else if (tl == (int)strlen(T_FSC) && strncmp(e->topic, T_FSC, tl) == 0) {
            char *j = strndup(e->data, dl);
            if (j) { fs_ops_submit(j); free(j); }   // SD file op; worker copies + runs off this stack
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

// ── Platform seams for the shared ha_ble_scan observer (ADR-0020) ────────────────
// (1) controller_init: on the panel the BLE controller lives on the C6 and is driven
//     over esp-hosted VHCI — bring it up before NimBLE (edge nodes pass NULL / native).
static esp_err_t panel_bt_controller_init(void)
{
    esp_err_t e = esp_hosted_bt_controller_init();
    if (e != ESP_OK) { ESP_LOGE(TAG, "hosted bt_controller_init failed 0x%x", e); return e; }
    e = esp_hosted_bt_controller_enable();
    if (e != ESP_OK) { ESP_LOGE(TAG, "hosted bt_controller_enable failed 0x%x", e); return e; }
    return ESP_OK;
}

// (2) on_reading sink: publish each fresh decoded meter as a canonical edge advert,
//     home/edge/<BLE_NODE>/<macflat>/adv, byte-for-byte the shape ha_mqtt.c emits on the
//     c3/c6/s3 nodes (schema 1, transport "ble-adv"). No ha_relay gate yet — the panel is
//     an unmanaged extra relay for now; ADR-0015 coverage assignment comes with the peer-
//     node migration. ts left empty: the panel has no wall-clock (no SNTP) and the edge
//     path already tolerates ts="" (ingest stamps arrival time). Float pre-formatted to
//     avoid any newlib nano %f dependency.
static void panel_publish_adv(const char *mac_str, const sb_reading_t *r, int rssi, void *user)
{
    (void)user;
    if (!s_client || !s_mqtt_up) return;

    char mf[13]; int j = 0;                       // macflat: strip the colons
    for (const char *p = mac_str; *p && j < 12; p++) if (*p != ':') mf[j++] = *p;
    mf[j] = '\0';

    char tbuf[16];                                // temperature, sign-safe, 1 decimal
    int t10 = (int)(r->temperature_c * 10.0f + (r->temperature_c >= 0 ? 0.5f : -0.5f));
    bool neg = t10 < 0; int at = neg ? -t10 : t10;
    snprintf(tbuf, sizeof(tbuf), "%s%d.%d", neg ? "-" : "", at / 10, at % 10);

    char metrics[96];
    if (r->battery_pct >= 0)
        snprintf(metrics, sizeof(metrics), "{\"temperature_c\":%s,\"humidity_pct\":%d,\"battery_pct\":%d}",
                 tbuf, r->humidity_pct, r->battery_pct);
    else
        snprintf(metrics, sizeof(metrics), "{\"temperature_c\":%s,\"humidity_pct\":%d}",
                 tbuf, r->humidity_pct);

    char topic[80];
    snprintf(topic, sizeof(topic), "home/edge/%s/%s/adv", BLE_NODE, mf);
    char payload[320];
    int n = snprintf(payload, sizeof(payload),
        "{\"schema\":1,\"node\":\"%s\",\"mac\":\"%s\",\"device_type\":\"%s\","
        "\"ts\":\"\",\"transport\":\"ble-adv\",\"metrics\":%s,\"meta\":{\"rssi\":%d}}",
        BLE_NODE, mac_str, r->device_type, metrics, rssi);
    if (n <= 0 || n >= (int)sizeof(payload)) return;
    esp_mqtt_client_publish(s_client, topic, payload, n, 1, false);
}

// ADR-0023 reach census: feed EVERY heard SwitchBot advert (allowlist-independent) into the
// per-MAC RSSI-EWMA table. Hot path — keep it cheap. The panel relays everything it hears, but
// the census is what makes that reach VISIBLE to the coordinator's best_relay (fossil-killer).
static void panel_on_sighting(const uint8_t mac[6], int rssi, void *user)
{
    (void)user;
    ha_reach_note(mac, rssi);
}

// Reach report sink: wrap ha_reach's JSON array and publish the canonical census topic. The
// dictator's ha-edge-mapper ingests home/edge/+/reach for ANY node (report is unsigned; only the
// coordinator's trigger is signed — which this unsigned-LAN panel neither receives nor needs).
static void reach_publish(const char *reach_json)
{
    if (!s_client || !s_mqtt_up || !reach_json) return;
    char topic[64];
    snprintf(topic, sizeof(topic), "home/edge/%s/reach", BLE_NODE);
    esp_mqtt_client_publish(s_client, topic, reach_json, 0, 0, false);
}

// Passive BLE relay: start the shared observer (VHCI controller + adv-publish sink),
// then publish a compact telemetry line every 2s. Own task — never blocks the MQTT
// callback. Non-fatal: if bring-up bailed we still publish running:false so it's visible.
static void ble_task(void *pv)
{
    ha_ble_scan_cfg_t cfg = {
        .controller_init = panel_bt_controller_init,   // VHCI (panel); edge passes NULL
        .on_reading      = panel_publish_adv,
        .on_sighting     = panel_on_sighting,          // ADR-0023 reach census tap
        .user            = NULL,
        // WiFi rides the C6 radio over esp-hosted; a continuous BLE scan starves the WiFi
        // beacon (bcn_timeout drops). Duty-cycle to ~40% so the MQTT/OTA lifeline holds —
        // mandatory now that BLE runs by default on wall power, not just on-demand.
        .shared_radio    = true,
    };
    ha_ble_scan_start(&cfg);
    // Census: autonomous fallback cadence (no signed trigger — this panel isn't an enrolled
    // signing node; the mapper ingests the fallback report all the same). Default 30 min window.
    ha_reach_cfg_t reach_cfg = { .node_id = BLE_NODE, .publish = reach_publish, .fallback_ms = 0 };
    ha_reach_start(&reach_cfg);
    for (;;) {
        uint32_t total = 0, decoded = 0; int8_t rssi = 0;
        ha_ble_scan_stats(&total, &decoded, &rssi);
        if (s_client && s_mqtt_up) {
            char msg[192];
            snprintf(msg, sizeof(msg),
                     "{\"build\":\"%s\",\"node\":\"%s\",\"running\":%s,\"adv_total\":%u,\"decoded\":%u,\"last_rssi\":%d}",
                     APP_BUILD_TAG, BLE_NODE, ha_ble_scan_running() ? "true" : "false",
                     (unsigned)total, (unsigned)decoded, rssi);
            esp_mqtt_client_publish(s_client, T_BLE, msg, 0, 1, 1);   // qos1 retained
        }
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}

// ── Power-aware BLE (Hugh's directive, 2026-07-02) ───────────────────────────────
// The panel relays BLE + runs the ADR-0023 reach census ONLY on wall power; on battery
// it pauses the radio so it never burns the cell relaying for its neighbours. On every
// power-context CHANGE it pushes an immediate notice (T_PWR + status) so the coordinator
// can rebalance the mesh around the panel's new availability, and on regaining wall power
// it fires a fresh reach report so best_relay re-includes the panel at once.
// (s_ble_started / s_on_wall / s_ble_relaying declared up top for the cmd/ble handler.)

// Bring up the BLE observer + reach census exactly once. Idempotent: safe to call from the
// power watcher and from a manual cmd/ble. ha_ble_scan_start itself guards re-entry, but the
// single ble_task (telemetry loop + ha_reach_start) must not be spawned twice.
static void ble_ensure_started(void)
{
    if (s_ble_started) return;
    s_ble_started = true;
    xTaskCreate(ble_task, "ble", 6144, NULL, 3, NULL);
}

// Announce a power-context transition to the broker (retained, so a late-subscribing
// coordinator still sees the current state). Edge-consumable schema for the pending
// "prompted rebalancing on known state change" coordinator handler (dev's).
static void power_ctx_publish(bool on_wall)
{
    if (!s_client || !s_mqtt_up) return;
    char msg[160];
    snprintf(msg, sizeof(msg),
        "{\"node\":\"%s\",\"event\":\"power_ctx\",\"on_wall\":%s,\"ble_relay\":%s}",
        BLE_NODE, on_wall ? "true" : "false", s_ble_relaying ? "true" : "false");
    esp_mqtt_client_publish(s_client, T_PWR, msg, 0, 1, 1);   // qos1 retained (legacy topic, kept through cutover)

    // Canonical node state-change envelope — docs/design/rollup-ladder-and-replica-sync.md Part C.
    // dev's coordinator event-reconcile consumes home/edge/+/event (ADR-0023 census inverted, node->server).
    // ts left empty: the panel has no wall-clock (no SNTP), same convention as the reach report — the
    // coordinator stamps arrival. censusing == relaying (the reach census runs with the wall-only relay).
    // Retained = current-state semantics so a reconnecting coordinator re-reads it.
    char etopic[64], emsg[224];
    snprintf(etopic, sizeof(etopic), "home/edge/%s/event", BLE_NODE);
    snprintf(emsg, sizeof(emsg),
        "{\"schema\":1,\"node\":\"%s\",\"kind\":\"power\",\"ts\":\"\","
        "\"state\":{\"on_wall\":%s,\"relaying\":%s,\"censusing\":%s}}",
        BLE_NODE, on_wall ? "true" : "false",
        s_ble_relaying ? "true" : "false", s_ble_relaying ? "true" : "false");
    esp_mqtt_client_publish(s_client, etopic, emsg, 0, 1, 1);   // qos1 retained
}

// Poll wall power; act only on the edge. Gated on MQTT so the very first transition (boot
// context established) actually reaches the broker. 3s poll ⇒ a plug/unplug is reflected in
// well under the 15s heartbeat, which is what makes it feel like a live "context change".
static void power_task(void *pv)
{
    ha_batt_sample_t bs = {0};
    for (;;) {
        if (s_mqtt_up && ha_battery_sample(&bs) == ESP_OK) {
            bool wall = bs.on_wall;
            if (wall != s_on_wall) {
                s_on_wall = wall;
                if (wall) {
                    if (!s_ble_started) ble_ensure_started();  // fresh start already scans
                    else                ha_ble_scan_resume();  // was paused on battery
                    s_ble_relaying = true;
                } else {
                    if (s_ble_started)  ha_ble_scan_pause();   // stop burning the cell
                    s_ble_relaying = false;
                }
                power_ctx_publish(wall);   // tell the coordinator NOW (retained)
                publish_status();          // status carries on_wall/gaining too
                if (wall && s_ble_started) ha_reach_report();  // push fresh reach so best_relay re-adds us
                ESP_LOGW(TAG, "power ctx -> %s; BLE relay %s",
                         wall ? "WALL" : "BATTERY", s_ble_relaying ? "ON" : "OFF");
            }
        }
        vTaskDelay(pdMS_TO_TICKS(3000));
    }
}

// C6 slave-OTA: the P4 fetches the matched 2.12.9 slave image (with CP_BT=y) over
// HTTP and writes it to the C6's inactive OTA slot over the SDIO link, then reboots
// the C6 into it. The C6's own partition scheme is used, so a layout mismatch errors
// rather than bricks. Reboots the C6 -> WiFi/SDIO blips briefly, then re-establishes.
#define SLV_CHUNK 4000
static void slave_ota_task(void *pv)
{
    char *url = (char *)pv;
    esp_err_t e = ESP_FAIL;
    int total = 0, r = 0;
    uint8_t *buf = NULL;
    bool began = false;
    ESP_LOGW(TAG, "C6 slave-OTA: fetching %s", url);
    if (s_client && s_mqtt_up) esp_mqtt_client_publish(s_client, T_SLVST, "{\"slave_ota\":\"begin\"}", 0, 1, 1);

    esp_http_client_config_t hc = { .url = url, .timeout_ms = 20000 };
    esp_http_client_handle_t cl = esp_http_client_init(&hc);
    if (!cl) goto done;
    if ((e = esp_http_client_open(cl, 0)) != ESP_OK) { ESP_LOGE(TAG, "http open 0x%x", e); goto cleanup; }
    esp_http_client_fetch_headers(cl);

    if ((e = esp_hosted_slave_ota_begin()) != ESP_OK) { ESP_LOGE(TAG, "slave_ota_begin 0x%x", e); goto cleanup; }
    began = true;
    buf = malloc(SLV_CHUNK);
    if (!buf) { e = ESP_ERR_NO_MEM; goto cleanup; }

    while ((r = esp_http_client_read(cl, (char *)buf, SLV_CHUNK)) > 0) {
        if ((e = esp_hosted_slave_ota_write(buf, r)) != ESP_OK) {
            ESP_LOGE(TAG, "slave_ota_write 0x%x @ %d", e, total); break;
        }
        total += r;
        if ((total % 131072) < SLV_CHUNK) {
            char pm[96]; snprintf(pm, sizeof(pm), "{\"slave_ota\":\"progress\",\"bytes\":%d}", total);
            if (s_client && s_mqtt_up) esp_mqtt_client_publish(s_client, T_SLVST, pm, 0, 0, 0);
            ESP_LOGW(TAG, "C6 slave-OTA progress %d", total);
        }
    }
    if (r < 0) { e = ESP_FAIL; ESP_LOGE(TAG, "http read err %d", r); }

    if (e == ESP_OK && r == 0 && total > 0) {
        if ((e = esp_hosted_slave_ota_end()) == ESP_OK) {
            ESP_LOGW(TAG, "C6 slave-OTA end OK (%d bytes); activating — C6 will reboot", total);
            e = esp_hosted_slave_ota_activate();   // reboots the C6 into the new slot
        } else ESP_LOGE(TAG, "slave_ota_end 0x%x", e);
    }
cleanup:
    if (buf) free(buf);
    if (cl) { esp_http_client_close(cl); esp_http_client_cleanup(cl); }
done:
    (void)began;
    { char msg[128];
      snprintf(msg, sizeof(msg), "{\"slave_ota\":\"%s\",\"err\":\"0x%x\",\"bytes\":%d}",
               e == ESP_OK ? "complete" : "failed", (int)e, total);
      ESP_LOGW(TAG, "C6 slave-OTA result: %s (0x%x, %d bytes)", e == ESP_OK ? "OK" : "FAIL", (int)e, total);
      if (s_client && s_mqtt_up) esp_mqtt_client_publish(s_client, T_SLVST, msg, 0, 1, 1); }
    free(url);
    vTaskDelete(NULL);
}

// I2C bus scan — probe both buses for ACKing addresses, publish the list. Own task
// (probe can briefly stretch), never the MQTT callback. Requires the display up so
// the I2C buses exist (auto-boots on MQTT connect anyway).
static void i2cscan_task(void *pv)
{
    char res[160];
    bsp_i2c_scan(res, sizeof(res));
    ESP_LOGW(TAG, "I2C scan: %s", res);
    if (s_client && s_mqtt_up) {
        char msg[224];
        snprintf(msg, sizeof(msg), "{\"i2c\":\"%s\"}", res);
        esp_mqtt_client_publish(s_client, T_I2CRES, msg, 0, 1, 1);
    }
    vTaskDelete(NULL);
}

static void battdump_task(void *pv)
{
    char res[224];
    ha_battery_dump(res, sizeof(res));
    ESP_LOGW(TAG, "battdump: %s", res);
    if (s_client && s_mqtt_up) {
        char msg[288];
        snprintf(msg, sizeof(msg), "{\"battdump\":\"%s\"}", res);
        esp_mqtt_client_publish(s_client, T_I2CRES, msg, 0, 1, 1);
    }
    vTaskDelete(NULL);
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

// Best-effort live mirror of a battery-profile row (SD write is the source of truth).
static void battprofile_publish(const char *json)
{
    if (s_client && s_mqtt_up) esp_mqtt_client_publish(s_client, T_BPROF, json, 0, 0, 0);
}

// SD file-op result → MQTT.
static void fs_publish(const char *json)
{
    if (s_client && s_mqtt_up) esp_mqtt_client_publish(s_client, T_FS, json, 0, 0, 0);
}

// Retained status of the ACTIVE battery profile (provenance + offsets + LUT + how it was set). Lets an
// operator confirm which curve a device is running and where it came from (ADR-0024: comparable/auditable).
static void publish_profile(void)
{
    ha_batt_profile_t p;
    if (!ha_battery_get_profile(&p)) return;
    char buf[640];
    int n = ha_batt_profile_rt_to_json(&p, s_profile_source, buf, sizeof(buf));
    if (n > 0 && s_client && s_mqtt_up)
        esp_mqtt_client_publish(s_client, T_PROF, buf, 0, 1, 1);   // qos1, retained
}

// Apply a pushed battery profile off the mqtt-callback stack (JSON parse + NVS write + hot-swap are too
// heavy for the event stack). Payload is a JSON profile, or the keyword "get" (re-publish status) or
// "default" (drop the stored profile, revert to the baked-in curve). Frees pv. This is the reflash-free
// deploy path (ADR-0024 §5): push a better curve as data; it hot-swaps live and persists across reboot.
static void profile_apply_task(void *pv)
{
    char *payload = (char *)pv;
    char res[96];
    if (strcmp(payload, "get") == 0) {
        publish_profile();
    } else if (strcmp(payload, "default") == 0) {
        ha_batt_profile_rt_clear();
        ha_batt_profile_t d = ha_batt_profile_d1001_default();
        ha_battery_set_profile(&d);
        strncpy(s_profile_source, "default", sizeof(s_profile_source) - 1);
        publish_profile();
        if (s_client && s_mqtt_up)
            esp_mqtt_client_publish(s_client, T_ACK, "{\"profile\":\"reset-to-default\"}", 0, 0, 0);
    } else {
        ha_batt_profile_t np;
        char err[64] = {0};
        if (ha_batt_profile_rt_from_json(payload, &np, err, sizeof(err)) == ESP_OK) {
            ha_battery_set_profile(&np);        // hot-swap into the gauge (race-safe under its mutex)
            ha_batt_profile_rt_save(&np);       // persist so it survives reboot
            strncpy(s_profile_source, "pushed", sizeof(s_profile_source) - 1);
            publish_profile();
            snprintf(res, sizeof(res), "{\"profile\":\"applied\",\"version\":\"%s\"}", np.version);
        } else {
            snprintf(res, sizeof(res), "{\"profile\":\"rejected\",\"err\":\"%s\"}", err);
        }
        if (s_client && s_mqtt_up) esp_mqtt_client_publish(s_client, T_ACK, res, 0, 0, 0);
    }
    free(payload);
    vTaskDelete(NULL);
}


// SNTP sync callback (roadmap #1): the system clock is already set by SNTP when this fires; persist
// the wall-clock time into the PCF8563 so the RTC becomes the reboot-holdover source (clears VL).
static void on_time_synced(struct timeval *tv)
{
    struct tm local;
    localtime_r(&tv->tv_sec, &local);          // store local time (mktime-symmetric on boot)
    esp_err_t e = ha_rtc_set(&local);
    ESP_LOGI(TAG, "clock: SNTP synced -> RTC set (%s)", esp_err_to_name(e));
}

// RTC + SNTP wall-clock backbone (ability G). Called from app_main after I2C1 + WiFi are up: seed the
// system clock from the RTC's holdover immediately (so the clock is right before SNTP returns), then
// start SNTP against the dictator; on_time_synced writes freshly-synced time back to the RTC.
static void clock_backbone_start(void)
{
    ha_rtc_init(&(ha_rtc_cfg_t){ .bus = bsp_i2c1(), .addr = HA_RTC_PCF8563_ADDR });
    setenv("TZ", PANEL_TZ, 1);
    tzset();
    if (!ha_rtc_present()) { ESP_LOGW(TAG, "clock: RTC not responding on I2C1"); return; }

    struct tm rtc_tm; bool rtc_valid = false;
    if (ha_rtc_get(&rtc_tm, &rtc_valid) == ESP_OK && rtc_valid) {
        rtc_tm.tm_isdst = -1;                  // let mktime resolve DST for the stored local time
        struct timeval tv = { .tv_sec = mktime(&rtc_tm), .tv_usec = 0 };
        settimeofday(&tv, NULL);
        ESP_LOGI(TAG, "clock: seeded system time from RTC holdover");
    } else {
        ESP_LOGW(TAG, "clock: RTC time not valid yet (VL set) — awaiting SNTP");
    }

    esp_sntp_config_t sc = ESP_NETIF_SNTP_DEFAULT_CONFIG(NTP_SERVER);
    sc.sync_cb = on_time_synced;
    esp_netif_sntp_init(&sc);
    ESP_LOGI(TAG, "clock: SNTP -> %s", NTP_SERVER);
}

// Presence + tap-to-wake (roadmap #3, ability A). ha_imu is already init'd (ha_battery_init composes
// on it); enable its hardware wake/tap engine and poll: a tap relights the panel, and motion publishes
// a presence pulse the coordinator times out with asymmetric dwell (mirrors the edge event-reconcile
// pattern — the panel reports "motion seen", the dictator owns the presence decision, ADR-0001).
static void presence_task(void *pv)
{
    ha_imu_events_enable(150);              // ~150 mg wake threshold (approach/touch)
    int64_t last_pub_us = 0;
    for (;;) {
        bool motion = false, tap = false;
        if (ha_imu_poll(&motion, &tap) == ESP_OK) {
            if (tap) bsp_display_wake();
            int64_t now = esp_timer_get_time();
            if (motion && now - last_pub_us > 15LL * 1000 * 1000 && s_client && s_mqtt_up) {
                last_pub_us = now;          // rate-limit the pulse to once / 15 s while motion continues
                char topic[64], js[112];
                snprintf(topic, sizeof topic, "home/edge/%s/presence", BLE_NODE);
                snprintf(js, sizeof js,
                         "{\"schema\":1,\"kind\":\"presence\",\"node\":\"%s\",\"present\":true}", BLE_NODE);
                esp_mqtt_client_publish(s_client, topic, js, 0, 0, false);
                ESP_LOGI(TAG, "presence: motion -> published");
            }
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

void app_main(void)
{
    bsp_display_predark();   // FIRST: hold the panel dark across boot (kills the OTA-reboot strobe)
    led_init();                                // red status LED off; used by the boot gate + warn
    s_pp_cfg = ha_power_policy_d1001_cfg();     // set before MQTT-connect can fire the boot gate
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
    ha_battery_cfg_t bcfg = ha_battery_d1001_cfg(bsp_io_expander(), bsp_i2c1());
    bcfg.display_on_fn = bsp_display_is_on;   // display state for SoC normalization (ADR-0024)
    // A profile persisted from an earlier MQTT push (ADR-0024 §5) wins over the baked-in default; a
    // missing/corrupt blob falls back to it. This is the reflash-free deploy path: the curve is data.
    static ha_batt_profile_t s_boot_prof;
    if (ha_batt_profile_rt_load(&s_boot_prof) == ESP_OK) {
        bcfg.profile = &s_boot_prof;
        strncpy(s_profile_source, "nvs", sizeof(s_profile_source) - 1);
    }
    ha_battery_init(&bcfg);                    // ADC/IMU/charge config (handles from the display BSP)
    s_batt_ready = true;
    publish_profile();                         // retained active-profile status (guards on s_mqtt_up)
    ha_battery_charge_start();                 // thermal-gated charger + restart watchdog
    ha_power_policy_monitor_start(&s_pp_cfg, &s_pp_io, 5000);   // battery safety monitor: warn @5-10%, hard-off @0%
    clock_backbone_start();                    // roadmap #1 (ability G): RTC holdover + SNTP wall clock
    xTaskCreate(presence_task, "presence", 3072, NULL, 3, NULL);   // roadmap #3 (ability A): IMU presence + tap-wake
    xTaskCreate(heartbeat_task, "hb", 4096, NULL, 3, NULL);
    xTaskCreate(button_task, "btn", 3072, NULL, 3, NULL);   // back-button screen toggle
    xTaskCreate(power_task, "pwr", 4096, NULL, 3, NULL);    // power-aware BLE: on wall / off battery + notify
    bat_profile_start(battprofile_publish);   // mount SD + log the battery discharge curve (non-fatal)
    fs_ops_start(fs_publish);                 // SD file-ops over MQTT (cmd/fs)
    // Display is NOT started here — trigger it over MQTT with cmd/display "on"
    // once the device is confirmed live, so a failed bring-up can't brick boot.
}
