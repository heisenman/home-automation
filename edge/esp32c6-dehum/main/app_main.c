// XIAO ESP32-C6 dehumidifier node (ADR-0041) — Aprilaire E070, External-mode dry contact.
//
// ⛔ THIS BUILD HAS NO CONTROL LOGIC, DELIBERATELY. It holds the relay de-energized, joins the air-gap,
// and reports for duty. Nothing more. ADR-0041 §7.3 says to characterize External-mode timing — the
// real anti-short-cycle and defrost intervals — BEFORE writing control logic, or our controller ends up
// fighting the unit's own internal protections. `ha_aprilaire_dehum` does not exist yet and inventing
// it from the datasheet would be exactly the speculative infrastructure ADR-0041 §3 refuses.
//
// What it IS for: making this board a managed fleet member NOW, while it is on the bench and cabled.
// A fresh node can reject every OTA on a dangling node_id and the fix cannot ship over the air, so the
// first image has to arrive by cable regardless. Spending that cable session here means the real
// dehumidifier logic can ship as an ordinary OTA once the bench work is done.
//
// Hardware:
//   XIAO D1 (GPIO1) ────> relay IN (active-high module, 10k pull-down IN->GND, NO contact)
//
// ⛔ THE RELAY POLARITY IS A FAILSAFE DECISION (ADR-0041 §"Relay outputs"). Closed = "call for
// dehumidification" in External mode, so OPEN is the safe state when this node is dead, unpowered or
// rebooting — which is why it is the NO contact and never NC. Wired to NC, a node outage would leave
// the dehumidifier called ON indefinitely, presenting as "it never stops" rather than as a node fault.
//
// ⚠️ Do not land the contact on the Aprilaire's DH terminals until §7.3 step 1 is done: meter DH-DH
// powered with the relay disconnected, AC and DC, and to chassis. Above ~30 V means stop and reassess.
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "aprilaire_sniff.h"
#include "ha_config.h"
#include "ha_dout.h"
#include "ha_mqtt.h"
#include "ha_ota.h"
#include "ha_sntp.h"
#include "ha_wifi.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#warning "secrets.h not found — copy secrets.example.h to secrets.h and fill it in (or provision NVS)."
#define HA_WIFI_SSID  ""
#define HA_WIFI_PSK   ""
#define HA_BROKER_URI "mqtt://192.168.1.200:1883"
#define HA_NODE_ID    "c6-dehum-bench"
#define HA_NTP_SERVER "192.168.1.245"
#endif
#ifndef HA_CMD_SECRET
#define HA_CMD_SECRET ""
#endif
#ifndef HA_OTA_HOST
#define HA_OTA_HOST "192.168.1.210"
#endif
#ifndef HA_MQTT_USER
#define HA_MQTT_USER ""
#endif
#ifndef HA_MQTT_PASS
#define HA_MQTT_PASS ""
#endif
#ifndef HA_FW_VERSION
#define HA_FW_VERSION "v6-apr-reply"
#endif

static const char *TAG = "ha_dehum";

// Aprilaire DH relay. Active-high module (bench-verified on its twin: IN floating leaves NO-COM open,
// IN high shorts it), NO contact, 10k external pull-down IN->GND.
//
// v2 (2026-10-04): driven ONLY as a LEASE — `dehum_call {min:1..60}` closes DH for that long; the server
// renews it while it still wants dehumidification. If the server, broker or network goes away the call
// simply runs out, so the fail-safe (not calling) needs nothing to keep working. ha_dout owns the line:
// fail-safe off, MIN_ON / MIN_OFF on top of the unit's own compressor protection, and an esp_timer
// backstop at each lease's end drives the pin low directly if the loop that ticks ha_dout ever wedges.
// The Aprilaire treats DH as a REQUEST (External mode): defrost, anti-short-cycle and the E8 inlet lockout
// still apply, so "called" never guarantees "running" — erv/dehum_pm watts is the independent check.
#define DH_RELAY_GPIO   GPIO_NUM_1     // silk D1
#define LEASE_MAX_MIN   60u
#define DH_MIN_ON_MS    (60u * 1000u)        // never a sub-minute blip into a compressor appliance
#define DH_MIN_OFF_MS   (5u * 60u * 1000u)   // rest between calls

#define HEARTBEAT_MS 30000

static ha_config_t s_cfg;
static ha_dout_t   s_dh;
static esp_timer_handle_t s_dh_backstop;
static SemaphoreHandle_t  s_dh_mu;
static uint32_t    s_lease_end_ms;

static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }

// Latch the safe level BEFORE enabling the driver, so enabling output cannot emit a pulse. The external
// pull-down covers the window from reset until this runs; this covers everything after.
static void relay_init_safe(void) {
    gpio_reset_pin(DH_RELAY_GPIO);
    gpio_set_level(DH_RELAY_GPIO, 0);
    gpio_set_direction(DH_RELAY_GPIO, GPIO_MODE_INPUT_OUTPUT);   // INPUT too: readback of the real pin
    gpio_set_pull_mode(DH_RELAY_GPIO, GPIO_PULLDOWN_ONLY);
    gpio_set_level(DH_RELAY_GPIO, 0);
}

static void dh_backstop_cb(void *arg) {
    (void)arg;
    gpio_set_level(DH_RELAY_GPIO, 0);   // no lock, no ha_dout: this path exists for when those are wedged
}

static void dh_apply(uint32_t t) {
    ha_dout_tick(&s_dh, t);
    gpio_set_level(DH_RELAY_GPIO, ha_dout_level(&s_dh) ? 1 : 0);
}

static int32_t lease_left_s(uint32_t t) {
    if (!gpio_get_level(DH_RELAY_GPIO)) return 0;
    int32_t left = (int32_t)(s_lease_end_ms - t);
    return left > 0 ? left / 1000 : 0;
}

// home/edge/<node>/dehum/adv. `dh_call` is the PIN as read back, not what we asked for.
static void publish_dehum(void) {
    char metrics[96], reg[64];
    uint32_t t = now_ms();
    snprintf(metrics, sizeof metrics, "{\"dh_call\":%s,\"call_left_s\":%ld}",
             gpio_get_level(DH_RELAY_GPIO) ? "true" : "false", (long)lease_left_s(t));
    snprintf(reg, sizeof reg, "%s-dehum", s_cfg.node_id);
    ha_mqtt_publish_node_sensor_ex("dehum", reg, "dehum_call", "gpio", metrics);
}

static void dh_task(void *arg) {
    (void)arg;
    int last = -1;
    uint32_t last_pub = 0;
    for (;;) {
        uint32_t t = now_ms();
        xSemaphoreTake(s_dh_mu, portMAX_DELAY);
        dh_apply(t);
        xSemaphoreGive(s_dh_mu);
        int lvl = gpio_get_level(DH_RELAY_GPIO);
        if (ha_mqtt_is_connected() && (lvl != last || (uint32_t)(t - last_pub) >= HEARTBEAT_MS)) {
            publish_dehum();                          // on every change, and every 30 s
            last = lvl;
            last_pub = t;
        }
        vTaskDelay(pdMS_TO_TICKS(200));
    }
}

// minutes 0 = release (deferred to MIN_ON if just closed); 1..LEASE_MAX_MIN = call / renew for that long.
static const char *dh_call(int minutes) {
    if (minutes < 0 || minutes > (int)LEASE_MAX_MIN) return "REFUSED (min must be 0..60)";
    xSemaphoreTake(s_dh_mu, portMAX_DELAY);
    uint32_t t = now_ms();
    esp_timer_stop(s_dh_backstop);
    ha_dout_rc_t rc;
    if (minutes == 0) {
        rc = ha_dout_set(&s_dh, false, t);
        if (rc == HA_DOUT_DEFERRED)
            esp_timer_start_once(s_dh_backstop, ((uint64_t)DH_MIN_ON_MS + 5000u) * 1000u);
    } else {
        uint32_t w = (uint32_t)minutes * 60000u;
        rc = ha_dout_pulse(&s_dh, w, t);
        if (rc == HA_DOUT_OK) {
            s_lease_end_ms = t + w;
            esp_timer_start_once(s_dh_backstop, ((uint64_t)w + 5000u) * 1000u);
        }
    }
    dh_apply(t);
    xSemaphoreGive(s_dh_mu);
    switch (rc) {
    case HA_DOUT_OK:       return "ok";
    case HA_DOUT_DEFERRED: return "ok — release deferred to the 60 s minimum on-time";
    // the rest also runs from BOOT on purpose: a power blip or an OTA mid-call must not restart the
    // compressor instantly (delay-on-break, as real HVAC controls do)
    case HA_DOUT_REJECTED: return minutes ? "REFUSED (resting: 5 min minimum off-time after a call or a reboot)"
                                          : "REFUSED";
    default:               return "REFUSED (faulted)";
    }
}

static bool on_cmd(const cJSON *cmd, void *user) {
    (void)user;
    const cJSON *op = cJSON_GetObjectItem(cmd, "op");
    if (!cJSON_IsString(op)) return false;

    if (strcmp(op->valuestring, "dehum_call") == 0) {
        const cJSON *m = cJSON_GetObjectItem(cmd, "min");
        int minutes = cJSON_IsNumber(m) ? m->valueint : -1;
        const char *r = dh_call(minutes);
        ha_mqtt_log("dehum: dehum_call min=%d -> %s (relay=%d)", minutes, r, gpio_get_level(DH_RELAY_GPIO));
        publish_dehum();
        return true;
    }
    if (strcmp(op->valuestring, "apr_txtest") == 0) {   // BENCH ONLY: {secs:1..30, byte:0..255 (default 0)}
        const cJSON *sv = cJSON_GetObjectItem(cmd, "secs"), *bv = cJSON_GetObjectItem(cmd, "byte");
        const char *r = aprilaire_sniff_txtest(cJSON_IsNumber(sv) ? sv->valueint : 5,
                                               cJSON_IsNumber(bv) ? bv->valueint : 0);
        ha_mqtt_log("apr-txtest: request -> %s", r);
        return true;
    }
    if (strcmp(op->valuestring, "apr_reply") == 0) {   // Model 76 emulation: {secs, on, dryness, rh_x10}
        const cJSON *sv = cJSON_GetObjectItem(cmd, "secs"), *ov = cJSON_GetObjectItem(cmd, "on"),
                    *dv = cJSON_GetObjectItem(cmd, "dryness"), *rv = cJSON_GetObjectItem(cmd, "rh_x10");
        const char *r = aprilaire_sniff_reply(cJSON_IsNumber(sv) ? sv->valueint : 60,
                                              cJSON_IsNumber(ov) ? ov->valueint : 0,
                                              cJSON_IsNumber(dv) ? dv->valueint : 4,
                                              cJSON_IsNumber(rv) ? rv->valueint : 500);
        ha_mqtt_log("apr-reply: request -> %s", r);
        return true;
    }
    if (strcmp(op->valuestring, "apr_sniff") == 0) {   // RS-485 A/B sniffer report now (also every 30 s)
        aprilaire_sniff_report();
        return true;
    }
    if (strcmp(op->valuestring, "dehum_status") == 0) {
        ha_mqtt_log("dehum: relay GPIO%d=%d lease_left=%lds build=%s", DH_RELAY_GPIO,
                    gpio_get_level(DH_RELAY_GPIO), (long)lease_left_s(now_ms()), HA_FW_VERSION);
        publish_dehum();
        return true;
    }
    return false;   // not ours — let ha_mqtt log it as unknown
}

static bool repoint_healthy(void *user) { (void)user; return ha_mqtt_is_connected(); }

void app_main(void) {
    // FIRST. Before NVS, before the radio, before anything that can block or fail.
    relay_init_safe();
    ha_dout_init(&s_dh, &(ha_dout_cfg_t){ .active_high = true, .min_on_ms = DH_MIN_ON_MS,
                                           .min_off_ms = DH_MIN_OFF_MS, .max_on_ms = 0 },   // lease = the cap
                 now_ms());
    s_dh_mu = xSemaphoreCreateMutex();
    esp_timer_create(&(esp_timer_create_args_t){ .callback = dh_backstop_cb, .name = "dh_backstop" },
                     &s_dh_backstop);

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    // STATIC, not stack: app_main() returns, but ha_ota's identity gate holds a pointer into cfg and
    // dereferences it later. A stack cfg dangles and the OTA gate reads a garbage node_id.
    ha_config_load(&s_cfg, &(ha_config_t){ .wifi_ssid = HA_WIFI_SSID, .wifi_psk = HA_WIFI_PSK,
        .broker_uri = HA_BROKER_URI, .node_id = HA_NODE_ID, .ntp_server = HA_NTP_SERVER,
        .ota_host = HA_OTA_HOST, .cmd_secret = HA_CMD_SECRET });

    char why[96];
    if (!ha_config_identity_ok(&s_cfg, why, sizeof(why))) {
        ESP_LOGE(TAG, "REFUSING TO START — %s. Re-flash with an NVS blob minted for this board.", why);
        while (1) vTaskDelay(pdMS_TO_TICKS(10000));
    }
    ha_config_repoint_boot_check();

    if (ha_wifi_connect(s_cfg.wifi_ssid, s_cfg.wifi_psk, 30000) != ESP_OK) {
        ESP_LOGE(TAG, "Wi-Fi connect failed — restarting in 10s");
        vTaskDelay(pdMS_TO_TICKS(10000));
        esp_restart();
    }

    // ADR-0036 L0: mint this node's command secret AFTER the radio is up (esp_random() is only a true
    // hardware RNG once it is) and BEFORE ha_mqtt_init consumes it.
    if (!ha_config_ensure_node_secret(&s_cfg))
        ESP_LOGE(TAG, "no command secret — node will reject every signed command (incl. OTA)");

    if (!ha_sntp_sync(s_cfg.ntp_server, 15000))
        ESP_LOGW(TAG, "SNTP not synced — signed commands will fail their freshness check until it is");
    ha_sntp_start_periodic(30 * 60 * 1000);

    ha_mqtt_init(&(ha_mqtt_cfg_t){ .cmd_secret = s_cfg.cmd_secret, .ota_host = s_cfg.ota_host,
        .mqtt_user = HA_MQTT_USER, .mqtt_pass = HA_MQTT_PASS, .fw_version = HA_FW_VERSION,
        .abilities = "dehum", .enable_reach = false, .on_cmd = on_cmd });
    ha_mqtt_start(s_cfg.broker_uri, s_cfg.node_id);

    xTaskCreate(dh_task, "dehum_dh", 4096, NULL, 5, NULL);
    // Listen-only sniff of the E070 Remote A/B bus (D10/D9, UART1). Independent of the DH relay: a failure
    // here only loses the diagnostic, never control.
    if (!aprilaire_sniff_start(ha_mqtt_log)) ESP_LOGE(TAG, "RS-485 sniffer failed to start (DH control unaffected)");

    ESP_LOGW(TAG, "dehum node up: node=%s broker=%s — DH relay GPIO%d, leased calls (1..%u min, "
                  "min-on %us, min-off %us)", s_cfg.node_id, s_cfg.broker_uri, DH_RELAY_GPIO,
             (unsigned)LEASE_MAX_MIN, (unsigned)(DH_MIN_ON_MS / 1000), (unsigned)(DH_MIN_OFF_MS / 1000));

    ha_ota_confirm_if_pending();
    ha_config_repoint_confirm(repoint_healthy, NULL, 60000);
}
