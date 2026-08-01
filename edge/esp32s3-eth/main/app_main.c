// ESP32-S3-POE-ETH edge node: passive SwitchBot BLE scanner that relays decoded readings to the
// dictator's MQTT broker. PREFERS wired Ethernet (W5500); auto-falls back to onboard Wi-Fi when no
// cable is present. Transport switching is INTERRUPT-DRIVEN (the W5500 raises INT on a link change →
// ESP-IDF ETHERNET_EVENT → we reboot to re-pick) — no polling loop. Same relay contract as the C6
// node (home/edge/<node>/<mac>/adv).
#include "nvs_flash.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_eth.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "ha_config.h"
#include "ha_eth.h"
#include "ha_wifi.h"
#include "ha_sntp.h"
#include "ha_mqtt.h"
#include "ha_ota.h"
#include "ha_led.h"
#include "ha_relay.h"
#include "ha_gas.h"
#include "ha_reach.h"
#include "ha_ble_scan.h"

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
#define HA_FW_VERSION "v21-s3gaspins"
#endif

static const char *TAG = "ha_edge";

// Operability-LED status hooks (S3-only): the shared ha_mqtt calls these on connect/disconnect and on an
// OTA failure. Other boards pass NULL. Keeps the LED policy — an S3-specific peripheral — out of ha_mqtt.
static void s3_led_ok(void *u)        { (void)u; ha_led_set(HA_LED_OK); }          // relaying normally → LED off
static void s3_led_mqtt_down(void *u) { (void)u; ha_led_set(HA_LED_MQTT_DOWN); }   // broker unreachable
static void s3_led_ota_fail(void *u)  { (void)u; ha_led_set(HA_LED_OTA_FAIL); }
// Wi-Fi link status (ADR-0020): the shared ha_wifi component reports up/down via this optional callback
// (was baked into the old ha_wifi fork). up → link back, broker pending (BLUE); down → reconnecting (AMBER).
static void s3_wifi_led(bool up)      { ha_led_set(up ? HA_LED_MQTT_DOWN : HA_LED_WIFI_DOWN); }

// Interrupt-driven transport switch: a reboot cleanly re-picks Ethernet-vs-Wi-Fi. The eth driver is
// left running even when no cable is present, so its CONNECTED interrupt can tell us one was plugged in.
static void on_eth_link_up(void *a, esp_event_base_t b, int32_t id, void *d) {
    ESP_LOGW(TAG, "Ethernet cable detected (link up) while on Wi-Fi — rebooting onto the wired link");
    esp_restart();
}
static void on_eth_link_down(void *a, esp_event_base_t b, int32_t id, void *d) {
    ESP_LOGW(TAG, "Ethernet link lost — rebooting to fall back to Wi-Fi");
    esp_restart();
}

// Edge publish sink for the shared ha_ble_scan observer (ADR-0020): Phase-B relay gate,
// then publish. Replaces the tail of the old fork ble_scan.c gap_event now that
// parse/decode/dedup live in the shared component. controller_init stays NULL (native).
static void edge_on_reading(const char *mac_str, const sb_reading_t *r, int rssi, void *user) {
    (void)user;
    if (ha_relay_allowed(mac_str))
        ha_mqtt_publish_reading(mac_str, r, rssi);
}

// Generic (non-SwitchBot) relay sink — e.g. the Aranet radon sensor over BLE5 extended advertising.
// Same Phase-B relay gate as edge_on_reading, then publish via the generic device path.
static void edge_on_device(const char *mac_str, const char *device_type, const char *metrics_json,
                           int rssi, void *user) {
    (void)user;
    if (ha_relay_allowed(mac_str))
        ha_mqtt_publish_device(mac_str, device_type, metrics_json, rssi);
}

// Reach-census tap (ADR-0023): every heard endpoint feeds the RSSI-EWMA table, regardless of the relay
// allowlist — so the coordinator sees this node's whole neighborhood, not just what it currently relays.
static void edge_on_sighting(const uint8_t mac[6], int rssi, void *user) {
    (void)user;
    ha_reach_note(mac, rssi);
}

// Repoint confirm health (DJ-19): "the move worked" == the broker on the new net is reachable.
static bool edge_repoint_healthy(void *user) { (void)user; return ha_mqtt_is_connected(); }

void app_main(void) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    // STATIC, not stack: app_main() RETURNS after setup, but consumers (ha_ota's identity gate, ha_reach)
    // hold POINTERS into cfg and dereference them LATER (e.g. at OTA time). A stack cfg dangles the moment
    // app_main returns → the OTA gate reads a garbage node_id ("unknown") and REJECTS every OTA (observed
    // on hbed_s3 v19: "OTA REJECTED ... this node=unknown"). The C6 fork hit + fixed this 2026-07-08; the
    // S3 fork still had the stack cfg. Because the running image rejects OTA, the fix ships by CABLE flash.
    static ha_config_t cfg;
    ha_config_load(&cfg, &(ha_config_t){ .wifi_ssid = HA_WIFI_SSID, .wifi_psk = HA_WIFI_PSK,
        .broker_uri = HA_BROKER_URI, .node_id = HA_NODE_ID, .ntp_server = HA_NTP_SERVER,
        .ota_host = HA_OTA_HOST, .cmd_secret = HA_CMD_SECRET });

    // Phase-0 identity gate (ADR-0036): refuse to run if this board is not the one the NVS blob was minted
    // for. With a GENERIC image the binary no longer carries a node identity, so this eFuse-MAC binding IS
    // the anti-cross-provisioning guard that the per-node build used to provide — it is stronger, because
    // it checks silicon rather than trusting a manifest line a human typed. Without it a generic S3 image
    // would happily adopt whatever blob it found. (The C6 has had this since 412da0a.)
    char why[96];
    if (!ha_config_identity_ok(&cfg, why, sizeof(why))) {
        ESP_LOGE(TAG, "REFUSING TO START — %s. Re-flash this board with an NVS blob minted for it.", why);
        while (1) vTaskDelay(pdMS_TO_TICKS(10000));
    }

    // Air-gap repoint safety (DJ-19): BEFORE the network comes up, count this boot if a repoint is pending
    // and revert to the last-good config after too many failures (a bad broker/creds self-heals over the
    // air). Transport-agnostic — works with this node's Ethernet-first / Wi-Fi-fallback path.
    ha_config_repoint_boot_check();

    // Install the ha_mqtt seam early — ha_mqtt_has_cmd_secret() is checked below (LED FATAL if un-enrolled),
    // before ha_mqtt_start. LED hooks are S3-specific; reach is wired on this node.
    //
    // cmd_secret points at `cfg`'s buffer (which is `static`, so the pointer stays valid) rather than at
    // the HA_CMD_SECRET literal. That aliasing is LOAD-BEARING on this board: unlike the C6, ha_mqtt_init
    // runs BEFORE network bring-up, but ADR-0036 Layer 0 can only mint a secret AFTER the radio is up.
    // Pointing at the buffer lets ha_config_ensure_node_secret() fill it in later and have ha_mqtt see it.
    // Resolve the gas chip BEFORE ha_mqtt_init so the hello advertises what this board ACTUALLY has.
    // Safe this early: probing is pure I2C and needs no radio. Precedence matches the C6 — NVS pin (what
    // the PWA flasher wrote) -> legacy compile-select from secrets.h -> AUTO, ask the hardware.
    // Pads are board-specific: Waveshare S3-ETH is GPIO42 (SDA) / GPIO41 (SCL); the C6's 22/23 DO NOT
    // EXIST on this chip. Autodetect also separates SGP40 from SGP41 (shared address 0x59 — a wrong guess
    // silently costs the NOx pixel; see sgp4x_identify).
    ha_gas_sensor_t gas = ha_gas_from_name(cfg.gas_sensor);
    if (gas == HA_GAS_SENSOR_AUTO) {
#if defined(HA_GAS_SGP30)
        gas = HA_GAS_SENSOR_SGP30;
#elif defined(HA_GAS_BME680)
        gas = HA_GAS_SENSOR_BME680;
#elif defined(HA_GAS_SGP41)
        gas = HA_GAS_SENSOR_SGP41;
#elif defined(HA_GAS_SGP40)
        gas = HA_GAS_SENSOR_SGP40;
#else
        gas = ha_gas_detect(42, 41);      // generic image, nothing pinned -> ask the hardware
#endif
    }

    ha_mqtt_init(&(ha_mqtt_cfg_t){ .cmd_secret = cfg.cmd_secret, .ota_host = cfg.ota_host,
        .mqtt_user = HA_MQTT_USER, .mqtt_pass = HA_MQTT_PASS, .fw_version = HA_FW_VERSION,
        // ADR-0036 intake: advertise what we RESOLVED, never a hardcoded guess. This said "sgp40_gas"
        // unconditionally, which a generic image must not do — a node claiming an ability it lacks gets
        // auto-registered into a device record that never produces data (the half-wired-record failure).
        // NULL for a board with no sensor: that is a legitimate relay-only node, not a broken one.
        .abilities = ha_gas_device_type(gas),
        .enable_reach = true, .on_connected = s3_led_ok, .on_disconnected = s3_led_mqtt_down,
        .ota_on_fail = s3_led_ota_fail });

    // Shared network init — exactly once; both transports attach to this stack + event loop.
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    // Operability LED up early: OFF when healthy, lights ONLY on a fault (see ha_led.h / FIRMWARE-GUIDE §6).
    ha_led_init();
    ha_wifi_set_status_cb(s3_wifi_led);   // this board has an LED — drive it from the shared ha_wifi (ADR-0020)
    // NB the un-enrolled FATAL check moved BELOW network bring-up (ADR-0036 L0): a generic image has no
    // secret until it can mint one, so checking here would flash RED on every first boot.

    // PREFER the wire: try Ethernet first (short wait — the W5500 link comes up fast with a cable in).
    bool net_ok = false, on_wifi = false;
    ESP_LOGI(TAG, "network: trying Ethernet (W5500) first...");
    if (ha_eth_connect(8000) == ESP_OK) {
        ESP_LOGI(TAG, "network: on Ethernet (wired)");
        // Cable pulled later → its DISCONNECTED interrupt reboots us onto Wi-Fi.
        ESP_ERROR_CHECK(esp_event_handler_register(ETH_EVENT, ETHERNET_EVENT_DISCONNECTED,
                                                   on_eth_link_down, NULL));
        net_ok = true;
    } else {
        ESP_LOGW(TAG, "network: no Ethernet link — coming up on Wi-Fi (%s)", cfg.wifi_ssid);
        if (ha_wifi_connect(cfg.wifi_ssid, cfg.wifi_psk, 30000) == ESP_OK) {
            ESP_LOGI(TAG, "network: on Wi-Fi");
            // Cable plugged in later → the W5500 link interrupt reboots us onto Ethernet (preferred).
            ESP_ERROR_CHECK(esp_event_handler_register(ETH_EVENT, ETHERNET_EVENT_CONNECTED,
                                                       on_eth_link_up, NULL));
            net_ok = true; on_wifi = true;     // Wi-Fi shares the radio → duty-cycle the BLE scan below
        }
    }
    if (!net_ok) {
        ha_led_set(HA_LED_NET_DOWN);     // neither Ethernet nor Wi-Fi → RED x2
        ESP_LOGE(TAG, "no network (neither Ethernet nor Wi-Fi) — restarting in 10s");
        vTaskDelay(pdMS_TO_TICKS(10000));
        esp_restart();
    }

    // ADR-0036 Layer 0 — mint this node's own command secret if it doesn't have one. MUST be here: AFTER
    // the network is up (esp_random() is only a true HW RNG once the radio is running) and BEFORE
    // ha_mqtt_start. Writes into cfg.cmd_secret, which ha_mqtt_init already aliased above. No-op for a
    // node that already has one, so the enrolled fleet is unaffected.
    if (!ha_config_ensure_node_secret(&cfg))
        ESP_LOGE(TAG, "no command secret — node will reject every signed command (incl. OTA)");
    if (!ha_mqtt_has_cmd_secret())
        ha_led_set(HA_LED_FATAL);    // still no command secret → can't accept commands/OTA; RED solid

    if (!ha_sntp_sync(cfg.ntp_server, 15000)) {
        ESP_LOGW(TAG, "SNTP not synced — readings ship without ts; mapper stamps on ingest");
    }
    ha_sntp_start_periodic(30 * 60 * 1000);   // re-sync every 30 min

    ha_relay_init();                // load the persisted Phase-B coverage allowlist (default: relay-all)
    ha_mqtt_start(cfg.broker_uri, cfg.node_id);
    ha_ble_scan_cfg_t scan_cfg = {
        .controller_init = NULL,          // native controller (nimble_port_init brings it up)
        .on_reading      = edge_on_reading,
        .on_device       = edge_on_device,     // non-SwitchBot devices (Aranet radon) — ext-adv builds only
        .on_sighting     = edge_on_sighting,   // ADR-0023 reach census tap (allowlist-independent)
        .shared_radio    = on_wifi,       // duty-cycle when on WiFi (shared radio); continuous when wired
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

    // Gas lane (I2C, independent of the radio) — no-op if no sensor is wired. `gas` was resolved above,
    // before ha_mqtt_init, so the hello and the running lane can never disagree.
    ha_gas_start(cfg.node_id, gas, 42, 41);   // cfg.node_id, NOT HA_NODE_ID — identity lives in NVS now

    // If we just booted a freshly-OTA'd image, self-test now and confirm-or-rollback.
    ha_ota_confirm_if_pending();

    // If we just booted from a repoint (DJ-19), wait for the broker on the new net; confirm-or-reboot.
    // Timeout -> reboot -> the early boot_check counts it -> revert after RP_MAX_TRIES. No-op if none pending.
    ha_config_repoint_confirm(edge_repoint_healthy, NULL, 60000);
}
