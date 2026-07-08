// ESP32-C6 edge node: passive SwitchBot BLE scanner that relays decoded readings to the
// dictator's MQTT broker. Foundational native-C firmware (ADR-0003 Wasm host is Phase 8).
#include "nvs_flash.h"
#include "esp_log.h"
#include "esp_system.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "ha_config.h"
#include "ha_wifi.h"
#include "ha_sntp.h"
#include "ha_mqtt.h"
#include "ha_ota.h"
#include "ha_relay.h"
#include "ha_gas.h"
#include "ha_reach.h"
#include "ble_scan.h"

// ha_config secrets seam (ADR-0020): the shared ha_config component stays secrets-free; app_main reads the
// board-local secrets.h and passes the compile-time defaults in (NVS then overlays them in production).
#if __has_include("secrets.h")
#include "secrets.h"
#else
#warning "secrets.h not found — copy secrets.example.h to secrets.h and fill it in (or provision NVS)."
#define HA_WIFI_SSID  ""
#define HA_WIFI_PSK   ""
#define HA_BROKER_URI "mqtt://192.168.0.245:1883"
#define HA_NODE_ID    "c6-bench"
#define HA_NTP_SERVER "pool.ntp.org"
#endif
// ha_mqtt seam: secrets.h provides these in production; fall back for a bench build. HA_FW_VERSION is board-set.
#ifndef HA_CMD_SECRET
#define HA_CMD_SECRET ""
#endif
#ifndef HA_OTA_HOST
#define HA_OTA_HOST "192.168.0.245"
#endif
#ifndef HA_MQTT_USER
#define HA_MQTT_USER ""
#endif
#ifndef HA_MQTT_PASS
#define HA_MQTT_PASS ""
#endif
#ifndef HA_FW_VERSION
#define HA_FW_VERSION "v16-ledoff"
#endif

static const char *TAG = "ha_edge";

// Edge publish sink for the shared ha_ble_scan observer (ADR-0020): Phase-B relay gate,
// then publish. Replaces the tail of the old fork ble_scan.c gap_event now that
// parse/decode/dedup live in the shared component. controller_init stays NULL (native).
static void edge_on_reading(const char *mac_str, const sb_reading_t *r, int rssi, void *user) {
    (void)user;
    if (ha_relay_allowed(mac_str))
        ha_mqtt_publish_reading(mac_str, r, rssi);
}

// Reach-census tap (ADR-0023): every heard endpoint feeds the RSSI-EWMA table, regardless of the relay
// allowlist — so the coordinator sees this node's whole neighborhood, not just what it currently relays.
static void edge_on_sighting(const uint8_t mac[6], int rssi, void *user) {
    (void)user;
    ha_reach_note(mac, rssi);
}

// Repoint confirm health (DJ-19): "the move worked" == the broker on the new net is reachable. Same
// signal ha_ota uses for its trial-image self-test.
static bool edge_repoint_healthy(void *user) { (void)user; return ha_mqtt_is_connected(); }

void app_main(void) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    // Kill the XIAO ESP32-C6 onboard USER LED (GPIO15, active-low) — edge nodes don't need it and a glowing
    // LED in a bedroom is a nuisance. If the visible RED LED is instead the hardware battery-CHARGE indicator
    // (wired to the charge IC, not a GPIO), this has no effect — that one can't be killed in software (tape
    // it). Empirical test: v16-ledoff.
    gpio_reset_pin(GPIO_NUM_15);
    gpio_set_direction(GPIO_NUM_15, GPIO_MODE_OUTPUT);
    gpio_set_level(GPIO_NUM_15, 1);           // active-low user LED → HIGH = off

    // STATIC, not stack: app_main() RETURNS after setup, but consumers (ha_ota's identity gate, ha_reach)
    // hold POINTERS into cfg and dereference them LATER (e.g. at OTA time). A stack cfg dangles the moment
    // app_main returns → the OTA gate reads garbage node_id ("unknown") and rejects every OTA. (2026-07-08:
    // hbed_c6 hit exactly this; older nodes only "worked" because their freed stack happened to survive.)
    static ha_config_t cfg;
    ha_config_load(&cfg, &(ha_config_t){ .wifi_ssid = HA_WIFI_SSID, .wifi_psk = HA_WIFI_PSK,
        .broker_uri = HA_BROKER_URI, .node_id = HA_NODE_ID, .ntp_server = HA_NTP_SERVER,
        .ota_host = HA_OTA_HOST });

    // Air-gap repoint safety (DJ-19): BEFORE we touch Wi-Fi, count this boot if a repoint is pending and
    // revert to the last-good config after too many failures. Must be here — a bad SSID fails the connect
    // below and reboots before any late hook, so only an early counter catches that failure mode.
    ha_config_repoint_boot_check();

    if (ha_wifi_connect(cfg.wifi_ssid, cfg.wifi_psk, 30000) != ESP_OK) {
        ESP_LOGE(TAG, "Wi-Fi connect failed — restarting in 10s");
        vTaskDelay(pdMS_TO_TICKS(10000));
        esp_restart();
    }

    if (!ha_sntp_sync(cfg.ntp_server, 15000)) {
        ESP_LOGW(TAG, "SNTP not synced — readings ship without ts; mapper stamps on ingest");
    }
    ha_sntp_start_periodic(30 * 60 * 1000);   // re-sync every 30 min (the C6 RTC drifts fast)

    ha_relay_init();                // load the persisted Phase-B coverage allowlist (default: relay-all)
    ha_mqtt_init(&(ha_mqtt_cfg_t){ .cmd_secret = HA_CMD_SECRET, .ota_host = cfg.ota_host,
        .mqtt_user = HA_MQTT_USER, .mqtt_pass = HA_MQTT_PASS, .fw_version = HA_FW_VERSION,
        .enable_reach = true });
    ha_mqtt_start(cfg.broker_uri, cfg.node_id);
    ha_ble_scan_cfg_t scan_cfg = {
        .controller_init = NULL,          // native controller (nimble_port_init brings it up)
        .on_reading      = edge_on_reading,
        .on_sighting     = edge_on_sighting,   // ADR-0023 reach census tap (allowlist-independent)
        .user            = NULL,
    };
    ha_ble_scan_start(&scan_cfg);
    ESP_LOGI(TAG, "edge node up: node=%s broker=%s", cfg.node_id, cfg.broker_uri);

    // Mesh reach census (ADR-0023): report the RSSI-EWMA neighborhood on a coordinator push (or a long
    // fallback). Publishes home/edge/<node>/reach; the trigger arrives on .../reach/req (handled in ha_mqtt).
    ha_reach_cfg_t reach_cfg = {
        .node_id     = cfg.node_id,
        .publish     = ha_mqtt_publish_reach,
        .fallback_ms = 0,                 // 0 => default (~30 min, ≈2× the 900 s coordinator cadence)
    };
    ha_reach_start(&reach_cfg);

    // Dual-role add-on: bring up the SGP-40 VOC gas lane alongside the BLE relay. No-op-safe —
    // if the sensor isn't wired it logs and returns, and the node keeps relaying BLE.
    ha_gas_start();

    // If we just booted a freshly-OTA'd image, self-test now and confirm-or-rollback.
    ha_ota_confirm_if_pending();

    // If we just booted from a repoint (DJ-19), wait for the broker on the new net; confirm-or-reboot.
    // Up to 60 s — a fresh network needs Wi-Fi assoc + SNTP + MQTT connect. Timeout -> reboot -> the early
    // boot_check counts it -> revert after RP_MAX_TRIES. No-op if no repoint is pending.
    ha_config_repoint_confirm(edge_repoint_healthy, NULL, 60000);
}
