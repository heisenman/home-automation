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
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "ha_config.h"
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
#define HA_FW_VERSION "v1-dehum-idle"
#endif

static const char *TAG = "ha_dehum";

// Aprilaire DH relay. Active-high module (bench-verified on its twin: IN floating leaves NO-COM open,
// IN high shorts it), NO contact, 10k external pull-down IN->GND.
//
// This build NEVER drives it high. When control logic arrives it wants ha_dout — which owns a per-line
// fail-safe state and an independent max-on cap — not a bare gpio_set_level().
#define DH_RELAY_GPIO   GPIO_NUM_1     // silk D1

#define HEARTBEAT_MS 30000

static ha_config_t s_cfg;

// Latch the safe level BEFORE enabling the driver, so enabling output cannot emit a pulse. The external
// pull-down covers the window from reset until this runs; this covers everything after.
static void relay_init_safe(void) {
    gpio_reset_pin(DH_RELAY_GPIO);
    gpio_set_level(DH_RELAY_GPIO, 0);
    gpio_set_direction(DH_RELAY_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_pull_mode(DH_RELAY_GPIO, GPIO_PULLDOWN_ONLY);
    gpio_set_level(DH_RELAY_GPIO, 0);
}

// Says what this node is and — more usefully — what it is NOT, so a human reading the log stream does
// not spend time wondering why a "dehum" node never actuates anything.
static void heartbeat_task(void *arg) {
    (void)arg;
    for (;;) {
        if (ha_mqtt_is_connected())
            ha_mqtt_log("dehum: idle build — relay GPIO%d held de-energized, no control logic "
                        "(ADR-0041 §7.3 not yet characterized)", DH_RELAY_GPIO);
        vTaskDelay(pdMS_TO_TICKS(HEARTBEAT_MS));
    }
}

// Read-only. There is deliberately no command to drive the relay: this build cannot actuate the
// Aprilaire at all, and that property should survive someone poking at MQTT.
static bool on_cmd(const cJSON *cmd, void *user) {
    (void)user;
    const cJSON *op = cJSON_GetObjectItem(cmd, "op");
    if (!cJSON_IsString(op)) return false;

    if (strcmp(op->valuestring, "dehum_status") == 0) {
        ha_mqtt_log("dehum: relay GPIO%d level=0 (de-energized), build=%s, control logic absent",
                    DH_RELAY_GPIO, HA_FW_VERSION);
        return true;
    }
    return false;   // not ours — let ha_mqtt log it as unknown
}

static bool repoint_healthy(void *user) { (void)user; return ha_mqtt_is_connected(); }

void app_main(void) {
    // FIRST. Before NVS, before the radio, before anything that can block or fail.
    relay_init_safe();

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

    // abilities is EMPTY on purpose: this node offers no device_type yet. Claiming "dehumidifier" would
    // make intake advertise a capability that does not exist behind it.
    ha_mqtt_init(&(ha_mqtt_cfg_t){ .cmd_secret = s_cfg.cmd_secret, .ota_host = s_cfg.ota_host,
        .mqtt_user = HA_MQTT_USER, .mqtt_pass = HA_MQTT_PASS, .fw_version = HA_FW_VERSION,
        .abilities = "", .enable_reach = false, .on_cmd = on_cmd });
    ha_mqtt_start(s_cfg.broker_uri, s_cfg.node_id);

    xTaskCreate(heartbeat_task, "dehum_hb", 4096, NULL, 4, NULL);

    ESP_LOGW(TAG, "dehum node up: node=%s broker=%s — IDLE BUILD, relay GPIO%d de-energized, "
                  "no control logic", s_cfg.node_id, s_cfg.broker_uri, DH_RELAY_GPIO);

    ha_ota_confirm_if_pending();
    ha_config_repoint_confirm(repoint_healthy, NULL, 60000);
}
