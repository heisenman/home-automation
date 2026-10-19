// IoTorero / Athom PG03V3-US16A smart plug (ESP32-C3, 4 MB) — ADR-0042.
//
// A metered, switchable mains outlet as a managed edge node: signed commands (ADR-0010), node-born secret +
// standby intake (ADR-0036), signed OTA from ha-2 (ADR-0020). It is a DUMB RELAY (ADR-0001): it reports
// power and does what it is told; the only decisions it takes locally are the two a server cannot take in
// time — the physical button and the over-current trip.
//
// Hardware (Athom's published config, athom-tech/esp32-configs athom-smart-plug.yaml; verified live on our
// unit at intake — ADR-0042 §Hardware):
//   GPIO5  relay, active-high           GPIO3  button, active-low (internal pull-up)
//   GPIO6  blue LED, active-low         GPIO20 CSE7766 TX -> our UART1 RX, 4800 8E1 (24-byte frame / ~50 ms)
//
// ⛔ THE RELAY'S SAFE STATE IS "KEEP DOING WHAT IT WAS DOING". These plugs feed appliances (ERV, Aprilaire)
// whose power must not blink because the plug's firmware rebooted or took an OTA. So, unlike every other
// relay in the fleet (ha_dout: fail-safe OFF, not reused here for exactly that reason), the pad is HELD
// (gpio_hold_en) — the level survives software/watchdog resets — and the logical state is mirrored in RTC
// memory so the next boot re-asserts it without a glitch. Only a real power loss resets it, and power-on
// follows the configured boot mode (default: restore the last commanded state, ON the very first time).
//
// First boot on a unit that arrived running Tasmota: ha_partmig rewrites the partition table to ours
// (partitions.csv) and restarts — see firmware/components/ha_partmig.
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "ha_config.h"
#include "ha_cse7766.h"
#include "ha_mqtt.h"
#include "ha_ota.h"
#include "ha_partmig.h"
#include "ha_sntp.h"
#include "ha_wifi.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#warning "secrets.h not found — copy secrets.example.h to secrets.h and fill it in (or provision NVS)."
#define HA_WIFI_SSID  ""
#define HA_WIFI_PSK   ""
#define HA_BROKER_URI "mqtt://192.168.1.200:1883"
#define HA_NODE_ID    "plug-bench"
#define HA_NTP_SERVER "192.168.1.210"
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
#define HA_FW_VERSION "v3-plug"
#endif

static const char *TAG = "ha_plug";

#define RELAY_GPIO   GPIO_NUM_5
#define BUTTON_GPIO  GPIO_NUM_3
#define LED_GPIO     GPIO_NUM_6
#define CSE_RX_GPIO  GPIO_NUM_20
#define CSE_UART     UART_NUM_1

#define TRIP_A            16.0f            // the plug's rating (US16A); sustained above -> relay off, latched
#define TRIP_FRAMES       20               // ~1 s of consecutive frames over the limit (inrush is shorter)
#define HEARTBEAT_MS      30000
#define CHANGE_MIN_MS     2000             // fastest a power change can trigger a publish
#define ENERGY_SAVE_MS    (15u * 60u * 1000u)
#define CYCLE_MAX_S       120

// NVS namespace "plug": boot (u8: 0 off, 1 on, 2 restore), last (u8), lock (u8), wh (u64 milli-Wh base)
#define NVS_NS "plug"
enum { BOOT_OFF = 0, BOOT_ON = 1, BOOT_RESTORE = 2 };

static ha_config_t s_cfg;
static SemaphoreHandle_t s_mu;

// relay — RTC mirror survives every reset except power-on; the pad hold keeps the level meanwhile
#define RTC_MAGIC 0x504C5547u   // "PLUG"
static RTC_NOINIT_ATTR uint32_t s_rtc_magic;
static RTC_NOINIT_ATTR uint32_t s_rtc_on;
static bool s_on, s_tripped, s_lock;
static uint8_t s_boot_mode = BOOT_RESTORE;
static esp_timer_handle_t s_cycle_timer;

// meter
static ha_cse_state_t s_cse;
static ha_cse_reading_t s_last;            // most recent good frame
static bool   s_meter_ok;                  // a good frame within the last 2 s
static uint32_t s_last_frame_ms;
static double s_p_sum, s_v_sum, s_i_sum;   // means since the last publish
static uint32_t s_n;
static uint64_t s_wh_base_milli;           // persisted energy before this boot (milli-Wh)

static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }

static void nvs_put_u8(const char *k, uint8_t v) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_u8(h, k, v);
    nvs_commit(h);
    nvs_close(h);
}

static uint8_t nvs_get_u8_or(const char *k, uint8_t dflt) {
    nvs_handle_t h;
    uint8_t v = dflt;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) { nvs_get_u8(h, k, &v); nvs_close(h); }
    return v;
}

// Drive the relay without a glitch: set the register level first, release the hold (the pad takes the
// register value, which already matches), re-arm the hold.
static void relay_drive(bool on) {
    gpio_set_level(RELAY_GPIO, on ? 1 : 0);
    gpio_set_direction(RELAY_GPIO, GPIO_MODE_INPUT_OUTPUT);
    gpio_hold_dis(RELAY_GPIO);
    gpio_set_level(RELAY_GPIO, on ? 1 : 0);
    gpio_hold_en(RELAY_GPIO);
    s_rtc_on = on;
    s_rtc_magic = RTC_MAGIC;
}

static void relay_set(bool on, const char *why) {
    xSemaphoreTake(s_mu, portMAX_DELAY);
    bool changed = on != s_on;
    s_on = on;
    relay_drive(on);
    if (on) s_tripped = false;                     // an explicit ON clears a trip latch
    xSemaphoreGive(s_mu);
    if (changed && s_boot_mode == BOOT_RESTORE) nvs_put_u8("last", on);
    if (changed) ha_mqtt_log("plug: relay %s (%s)", on ? "ON" : "OFF", why);
}

// Boot: decide the level, then take the pin over from the hold without a pulse.
static void relay_boot(void) {
    esp_reset_reason_t rr = esp_reset_reason();
    bool warm = rr != ESP_RST_POWERON && rr != ESP_RST_BROWNOUT && s_rtc_magic == RTC_MAGIC;
    bool on;
    if (warm) {
        on = s_rtc_on != 0;                        // software/WDT/panic reset: keep what we had
    } else {
        s_boot_mode = nvs_get_u8_or("boot", BOOT_RESTORE);
        on = s_boot_mode == BOOT_ON ? true : s_boot_mode == BOOT_OFF ? false : nvs_get_u8_or("last", 1) != 0;
    }
    s_boot_mode = nvs_get_u8_or("boot", BOOT_RESTORE);
    s_lock = nvs_get_u8_or("lock", 0) != 0;
    s_on = on;
    relay_drive(on);
    ESP_LOGW(TAG, "relay %s at boot (%s reset, boot mode %d)", on ? "ON" : "OFF", warm ? "warm" : "cold",
             s_boot_mode);
}

static void cycle_cb(void *arg) { (void)arg; relay_set(true, "power-cycle done"); }

// ── meter ────────────────────────────────────────────────────────────────────────────────────────────────
static double energy_kwh(void) { return ((double)s_wh_base_milli / 1000.0 + ha_cse_energy_wh(&s_cse)) / 1000.0; }

static void energy_save(void) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_u64(h, "wh", (uint64_t)(energy_kwh() * 1e6));
    nvs_commit(h);
    nvs_close(h);
}

static void publish_plug(void) {
    char m[320], reg[64];
    xSemaphoreTake(s_mu, portMAX_DELAY);
    double p = s_n ? s_p_sum / s_n : s_last.power_w, v = s_n ? s_v_sum / s_n : s_last.voltage_v,
           i = s_n ? s_i_sum / s_n : s_last.current_a;
    s_p_sum = s_v_sum = s_i_sum = 0;
    s_n = 0;
    bool on = s_on, tripped = s_tripped, ok = s_meter_ok;
    xSemaphoreGive(s_mu);
    // PF only when it is computable: below the 50 mA current floor with a few watts flowing there is no
    // honest value, and a sentinel would be stored as a measurement — omit the key instead
    double va = v * i;
    char pf[32] = "";
    if (va > 0.5) snprintf(pf, sizeof pf, ",\"power_factor\":%.2f", fmin(1.0, p / va));
    else if (p < 0.5) snprintf(pf, sizeof pf, ",\"power_factor\":1.00");          // no load
    if (ok)
        snprintf(m, sizeof m, "{\"relay_on\":%s,\"power_w\":%.1f,\"voltage_v\":%.1f,\"current_a\":%.3f,"
                 "\"apparent_va\":%.1f%s,\"energy_kwh\":%.4f,\"tripped\":%s,\"meter_ok\":true}",
                 on ? "true" : "false", p, v, i, va, pf, energy_kwh(), tripped ? "true" : "false");
    else   // never publish a stale or invented reading as a measurement
        snprintf(m, sizeof m, "{\"relay_on\":%s,\"tripped\":%s,\"meter_ok\":false}", on ? "true" : "false",
                 tripped ? "true" : "false");
    snprintf(reg, sizeof reg, "%s-plug", s_cfg.node_id);
    ha_mqtt_publish_node_sensor_ex("plug", reg, "power_plug", "uart", m);
}

static void meter_task(void *arg) {
    (void)arg;
    uart_config_t uc = { .baud_rate = 4800, .data_bits = UART_DATA_8_BITS, .parity = UART_PARITY_EVEN,
                         .stop_bits = UART_STOP_BITS_1, .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
                         .source_clk = UART_SCLK_DEFAULT };
    if (uart_driver_install(CSE_UART, 512, 0, 0, NULL, 0) != ESP_OK || uart_param_config(CSE_UART, &uc) != ESP_OK ||
        uart_set_pin(CSE_UART, UART_PIN_NO_CHANGE, CSE_RX_GPIO, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE) != ESP_OK) {
        ESP_LOGE(TAG, "CSE7766 UART init failed — no metering (relay control unaffected)");
        vTaskDelete(NULL);
    }
    ha_cse_framer_t fr = {0};
    int over = 0;
    uint8_t buf[64];
    for (;;) {
        int n = uart_read_bytes(CSE_UART, buf, sizeof buf, pdMS_TO_TICKS(100));
        uint32_t t = now_ms();
        if (n <= 0) {
            if ((uint32_t)(t - s_last_frame_ms) > 500) fr.idx = 0;   // resync after silence
            if ((uint32_t)(t - s_last_frame_ms) > 2000) s_meter_ok = false;
            continue;
        }
        for (int k = 0; k < n; k++) {
            if (!ha_cse_feed(&fr, buf[k])) continue;
            ha_cse_reading_t r;
            xSemaphoreTake(s_mu, portMAX_DELAY);
            ha_cse_parse(&s_cse, fr.buf, &r);
            if (r.ok) {
                s_last = r;
                s_meter_ok = true;
                s_last_frame_ms = t;
                s_p_sum += r.power_w;
                s_v_sum += r.voltage_v;
                s_i_sum += r.current_a;
                s_n++;
            }
            xSemaphoreGive(s_mu);
            // over-current: the one protection that cannot wait for a server
            over = (r.ok && r.current_a > TRIP_A) ? over + 1 : 0;
            if (over >= TRIP_FRAMES && s_on) {
                relay_set(false, "OVER-CURRENT TRIP");
                xSemaphoreTake(s_mu, portMAX_DELAY);
                s_tripped = true;
                xSemaphoreGive(s_mu);
                ha_mqtt_log("plug: TRIPPED at %.2f A (limit %.1f A) — send plug_set on to re-arm", r.current_a, TRIP_A);
                over = 0;
            }
        }
    }
}

// ── publish loop, button, LED ────────────────────────────────────────────────────────────────────────────
static void plug_task(void *arg) {
    (void)arg;
    uint32_t last_pub = 0, last_save = now_ms(), press_ms = 0, led_t = 0;
    bool last_on = !s_on, pressed = false, led = false;
    float last_p = -1000;
    for (;;) {
        uint32_t t = now_ms();
        // button: debounced 20 ms poll; a short press toggles (ignored while child-locked)
        bool down = gpio_get_level(BUTTON_GPIO) == 0;
        if (down && !pressed) { pressed = true; press_ms = t; }
        else if (!down && pressed) {
            pressed = false;
            uint32_t held = t - press_ms;
            if (held >= 40 && held < 1500) {
                if (s_lock) ha_mqtt_log("plug: button press ignored (child lock)");
                else relay_set(!s_on, "button");
            }
        }
        // LED: dark when healthy (a lit plug at night is the Levoit lesson); 1 Hz blink while the broker is
        // unreachable, fast blink while tripped
        bool want;
        if (s_tripped) want = ((t / 150) & 1);
        else if (!ha_mqtt_is_connected()) want = ((t / 500) & 1);
        else want = false;
        if (want != led || (uint32_t)(t - led_t) > 5000) { gpio_set_level(LED_GPIO, want ? 0 : 1); led = want; led_t = t; }

        if (ha_mqtt_is_connected()) {
            float p = s_last.power_w;
            bool big = fabsf(p - last_p) > fmaxf(5.0f, 0.1f * fabsf(last_p));
            if (s_on != last_on || (uint32_t)(t - last_pub) >= HEARTBEAT_MS ||
                (big && (uint32_t)(t - last_pub) >= CHANGE_MIN_MS)) {
                publish_plug();
                last_on = s_on;
                last_p = p;
                last_pub = t;
            }
        }
        if ((uint32_t)(t - last_save) >= ENERGY_SAVE_MS) { energy_save(); last_save = t; }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

// ── signed commands (ADR-0010; ha_mqtt verifies before this runs) ───────────────────────────────────────
static bool on_cmd(const cJSON *cmd, void *user) {
    (void)user;
    const cJSON *op = cJSON_GetObjectItem(cmd, "op");
    if (!cJSON_IsString(op)) return false;

    if (strcmp(op->valuestring, "plug_set") == 0) {           // {on: bool}
        const cJSON *on = cJSON_GetObjectItem(cmd, "on");
        if (!cJSON_IsBool(on)) { ha_mqtt_log("plug_set: REFUSED (on must be true/false)"); return true; }
        esp_timer_stop(s_cycle_timer);
        relay_set(cJSON_IsTrue(on), "command");   // plug_task publishes the change
        return true;
    }
    if (strcmp(op->valuestring, "plug_cycle") == 0) {         // {off_s: 1..120}: off, then back on
        const cJSON *s = cJSON_GetObjectItem(cmd, "off_s");
        int secs = cJSON_IsNumber(s) ? s->valueint : 0;
        if (secs < 1 || secs > CYCLE_MAX_S) { ha_mqtt_log("plug_cycle: REFUSED (off_s 1..%d)", CYCLE_MAX_S); return true; }
        relay_set(false, "power-cycle");
        esp_timer_stop(s_cycle_timer);
        esp_timer_start_once(s_cycle_timer, (uint64_t)secs * 1000000u);
        return true;
    }
    if (strcmp(op->valuestring, "plug_cfg") == 0) {           // {boot: "on"|"off"|"restore", lock: bool}
        const cJSON *b = cJSON_GetObjectItem(cmd, "boot"), *l = cJSON_GetObjectItem(cmd, "lock");
        if (cJSON_IsString(b)) {
            int m = !strcmp(b->valuestring, "on") ? BOOT_ON : !strcmp(b->valuestring, "off") ? BOOT_OFF
                  : !strcmp(b->valuestring, "restore") ? BOOT_RESTORE : -1;
            if (m < 0) { ha_mqtt_log("plug_cfg: REFUSED (boot on|off|restore)"); return true; }
            s_boot_mode = (uint8_t)m;
            nvs_put_u8("boot", s_boot_mode);
            if (m == BOOT_RESTORE) nvs_put_u8("last", s_on);
        }
        if (cJSON_IsBool(l)) { s_lock = cJSON_IsTrue(l); nvs_put_u8("lock", s_lock); }
        ha_mqtt_log("plug_cfg: boot=%d lock=%d", s_boot_mode, s_lock);
        return true;
    }
    if (strcmp(op->valuestring, "plug_status") == 0) {
        ha_mqtt_log("plug: relay=%d tripped=%d lock=%d boot=%d meter_ok=%d P=%.1fW V=%.1f I=%.3fA E=%.4fkWh "
                    "status=0x%02X build=%s", s_on, s_tripped, s_lock, s_boot_mode, s_meter_ok, s_last.power_w,
                    s_last.voltage_v, s_last.current_a, energy_kwh(), s_last.status, HA_FW_VERSION);
        publish_plug();
        return true;
    }
    return false;   // not ours — ha_mqtt logs it as unknown
}

static bool repoint_healthy(void *user) { (void)user; return ha_mqtt_is_connected(); }

extern const uint8_t partition_table_bin_start[] asm("_binary_partition_table_bin_start");
extern const uint8_t partition_table_bin_end[] asm("_binary_partition_table_bin_end");

void app_main(void) {
    s_mu = xSemaphoreCreateMutex();

    // FIRST: a unit arriving from Tasmota gets our partition table (restarts; never returns if it migrates).
    // Before NVS on purpose — the migration erases Tasmota's NVS.
    ha_partmig_run(partition_table_bin_start, (size_t)(partition_table_bin_end - partition_table_bin_start), NULL);

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }
    relay_boot();
    gpio_config(&(gpio_config_t){ .pin_bit_mask = 1ULL << BUTTON_GPIO, .mode = GPIO_MODE_INPUT,
                                  .pull_up_en = GPIO_PULLUP_ENABLE });
    gpio_set_level(LED_GPIO, 1);                                   // active-low: off
    gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT);
    esp_timer_create(&(esp_timer_create_args_t){ .callback = cycle_cb, .name = "plug_cycle" }, &s_cycle_timer);
    {
        nvs_handle_t h;
        if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) { nvs_get_u64(h, "wh", &s_wh_base_milli); nvs_close(h); }
    }
    xTaskCreate(meter_task, "plug_meter", 4096, NULL, 6, NULL);
    xTaskCreate(plug_task, "plug", 4096, NULL, 5, NULL);

    // STATIC cfg: ha_ota's identity gate keeps a pointer into it (see edge/esp32c6-dehum).
    ha_config_load(&s_cfg, &(ha_config_t){ .wifi_ssid = HA_WIFI_SSID, .wifi_psk = HA_WIFI_PSK,
        .broker_uri = HA_BROKER_URI, .node_id = HA_NODE_ID, .ntp_server = HA_NTP_SERVER,
        .ota_host = HA_OTA_HOST, .cmd_secret = HA_CMD_SECRET });

    char why[96];
    if (!ha_config_identity_ok(&s_cfg, why, sizeof(why))) {
        // the relay + button + trip keep working; only the network side refuses to start as someone else
        ESP_LOGE(TAG, "REFUSING TO JOIN — %s. Re-flash with an NVS blob minted for this board.", why);
        while (1) vTaskDelay(pdMS_TO_TICKS(10000));
    }
    ha_config_repoint_boot_check();

    if (ha_wifi_connect(s_cfg.wifi_ssid, s_cfg.wifi_psk, 30000) != ESP_OK) {
        // safe to restart: the relay pad is held and the RTC mirror restores it — the load never blinks
        ESP_LOGE(TAG, "Wi-Fi connect failed — restarting in 10s");
        vTaskDelay(pdMS_TO_TICKS(10000));
        esp_restart();
    }

    // ADR-0036 L0: mint the command secret after the radio is up (true RNG), before ha_mqtt_init.
    if (!ha_config_ensure_node_secret(&s_cfg))
        ESP_LOGE(TAG, "no command secret — node will reject every signed command (incl. OTA)");

    if (!ha_sntp_sync(s_cfg.ntp_server, 15000))
        ESP_LOGW(TAG, "SNTP not synced — signed commands will fail their freshness check until it is");
    ha_sntp_start_periodic(30 * 60 * 1000);

    ha_mqtt_init(&(ha_mqtt_cfg_t){ .cmd_secret = s_cfg.cmd_secret, .ota_host = s_cfg.ota_host,
        .mqtt_user = HA_MQTT_USER, .mqtt_pass = HA_MQTT_PASS, .fw_version = HA_FW_VERSION,
        .abilities = "plug", .enable_reach = false, .on_cmd = on_cmd });
    ha_mqtt_start(s_cfg.broker_uri, s_cfg.node_id);

    ESP_LOGW(TAG, "plug node up: node=%s broker=%s relay=%s", s_cfg.node_id, s_cfg.broker_uri, s_on ? "ON" : "OFF");

    ha_ota_confirm_if_pending();
    ha_config_repoint_confirm(repoint_healthy, NULL, 60000);
}
