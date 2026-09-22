// ESP32-S3 shade node (ADR-0041) — drives a Gaposa QCTZ36SDU dry-contact panel.
//
// Thin platform shim, by design (ADR-0020): the decisions live in the shared components. `ha_gaposa`
// plans commands and models position; `ha_dout` owns each physical line's fail-safe state and hard
// max-on cap; this file wires them to 18 GPIOs, MQTT and NVS. No protocol or policy here.
//
// ⛔ THE HAZARD THIS NODE EXISTS AROUND. The QCT panel latches an error — all LEDs on, transmission
// STOPS — if a contact is held past 30 s, or if two channels are driven with DIFFERENT commands at the
// same instant. `ha_gaposa` serialises mixed commands and bounds every pulse; `ha_dout` enforces a second,
// independent cap per line so a wedged planner still cannot hold a contact. Both layers are deliberate.
//
// Hardware (docs/design/hvac-shade-device-integration.md §3):
//   S3 GPIO -> ULN2803A input -> output sinks the QCT terminal to `com` (16.55 V rail, ~15 mA per LED).
//   Sinking + inverting driver, so GPIO HIGH = contact asserted => ha_dout active_high = true.
//   A floating ULN2803 input cannot turn on (2.7 k into two base-emitter junctions), so the outputs are
//   inherently safe through reset — but we still latch LOW before enabling the drivers.
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
#include "ha_dout.h"
#include "ha_gaposa.h"
#include "ha_led.h"
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
#define HA_NODE_ID    "s3-shades-bench"
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
#define HA_FW_VERSION "v1-shades"
#endif

static const char *TAG = "ha_shades";

// ── pin map ───────────────────────────────────────────────────────────────────
// [channel][0 = Up, 1 = St, 2 = Dw]. Matches the ULN2803A allocation in the design doc: two shade
// channels per chip, so a wiring error stays local.
//
// ⚠️ Every pin here is deliberately OFF the S3's unusable and strapping sets. GPIO 26–32 are the SPI
// flash and 33–37 are consumed by the N16R8's OCTAL PSRAM — wiring there crashes the chip on boot. And a
// strapping pin (0/3/45/46) driving a shade contact would fire a command on every reset and every OTA,
// which is exactly the failure ha_dout's header warns about. Do not "tidy" these into a contiguous run.
#define SHADE_UP  0
#define SHADE_ST  1
#define SHADE_DW  2
static const gpio_num_t kPin[HA_GAPOSA_MAX_CH][3] = {
    { GPIO_NUM_1,  GPIO_NUM_2,  GPIO_NUM_4  },   // CH1  — U1 IN1..IN3
    { GPIO_NUM_5,  GPIO_NUM_6,  GPIO_NUM_7  },   // CH2  — U1 IN4..IN6
    { GPIO_NUM_8,  GPIO_NUM_9,  GPIO_NUM_10 },   // CH3  — U2 IN1..IN3
    { GPIO_NUM_11, GPIO_NUM_12, GPIO_NUM_13 },   // CH4  — U2 IN4..IN6
    { GPIO_NUM_14, GPIO_NUM_15, GPIO_NUM_16 },   // CH5  — U3 IN1..IN3
    { GPIO_NUM_17, GPIO_NUM_18, GPIO_NUM_21 },   // CH6  — U3 IN4..IN6
};

#define SHADE_CHANNELS   6
#define TICK_MS         20      // 50 Hz — fine enough to place a 500 ms pulse within half a percent
#define TELEMETRY_MS 30000

// Second, independent ceiling on any single contact. Well under the panel's 30 s lockout and comfortably
// above the 3 s interim hold, so it can only ever fire when something upstream has actually wedged.
#define LINE_MAX_ON_MS 6000

static ha_gaposa_t s_planner;
static ha_dout_t   s_line[HA_GAPOSA_MAX_CH][3];
static ha_config_t s_cfg;

static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }

// ── calibration (travel times) ────────────────────────────────────────────────
// Per-shade full-travel times are a property of the installation, not the firmware, and they differ per
// direction (gravity assists DOWN). Kept in NVS and settable over MQTT so calibrating a shade never
// requires a rebuild-and-reflash. Zero means "not calibrated" — ha_gaposa then reports position UNKNOWN
// rather than inventing one, which is the honest default.
static void cal_load(ha_gaposa_cfg_t *cfg) {
    nvs_handle_t h;
    if (nvs_open("shades", NVS_READONLY, &h) != ESP_OK) return;
    for (int i = 0; i < SHADE_CHANNELS; i++) {
        char k[8];
        uint32_t v = 0;
        snprintf(k, sizeof k, "up%d", i);
        if (nvs_get_u32(h, k, &v) == ESP_OK) cfg->travel_up_ms[i] = v;
        v = 0;
        snprintf(k, sizeof k, "dn%d", i);
        if (nvs_get_u32(h, k, &v) == ESP_OK) cfg->travel_down_ms[i] = v;
    }
    nvs_close(h);
}

static void cal_store(int ch, uint32_t up_ms, uint32_t down_ms) {
    nvs_handle_t h;
    if (nvs_open("shades", NVS_READWRITE, &h) != ESP_OK) return;
    char k[8];
    snprintf(k, sizeof k, "up%d", ch);
    nvs_set_u32(h, k, up_ms);
    snprintf(k, sizeof k, "dn%d", ch);
    nvs_set_u32(h, k, down_ms);
    nvs_commit(h);
    nvs_close(h);
}

// ── command dispatch ─────────────────────────────────────────────────────────
// Reached only after ha_mqtt's full ADR-0010 gate: signature verified, fresh, (ts,seq) strictly newer.
//
//   {"op":"shade","ch":1,"cmd":"up"}      ch 1..6, or 0 for ALL. cmd: up|down|stop|interim
//   {"op":"shade_cal","ch":1,"up_ms":25000,"down_ms":22000}
//
// "ch":0 fans one command across every channel — which ha_gaposa batches into a single transmission,
// the efficient path the panel explicitly allows for same-command groups.
static ha_gaposa_cmd_t parse_cmd(const char *s) {
    if (!s) return HA_GAPOSA_CMD_NONE;
    if (strcmp(s, "up")      == 0) return HA_GAPOSA_CMD_UP;
    if (strcmp(s, "down")    == 0) return HA_GAPOSA_CMD_DOWN;
    if (strcmp(s, "stop")    == 0) return HA_GAPOSA_CMD_STOP;
    if (strcmp(s, "interim") == 0) return HA_GAPOSA_CMD_INTERIM;
    return HA_GAPOSA_CMD_NONE;
}

static bool on_cmd(const cJSON *cmd, void *user) {
    (void)user;
    const cJSON *op = cJSON_GetObjectItem(cmd, "op");
    if (!cJSON_IsString(op)) return false;

    if (strcmp(op->valuestring, "shade") == 0) {
        const cJSON *jch = cJSON_GetObjectItem(cmd, "ch");
        const cJSON *jc  = cJSON_GetObjectItem(cmd, "cmd");
        int ch = cJSON_IsNumber(jch) ? (int)jch->valuedouble : -1;
        ha_gaposa_cmd_t c = parse_cmd(cJSON_IsString(jc) ? jc->valuestring : NULL);

        if (c == HA_GAPOSA_CMD_NONE || ch < 0 || ch > SHADE_CHANNELS) {
            ha_mqtt_log("shade: bad args (ch=%d cmd=%s)", ch,
                        cJSON_IsString(jc) ? jc->valuestring : "?");
            return true;    // ours, and rejected — don't fall through to "unknown cmd"
        }
        if (ch == 0) {
            for (int i = 0; i < SHADE_CHANNELS; i++) ha_gaposa_command(&s_planner, i, c, now_ms());
            ha_mqtt_log("shade: all <- %s", jc->valuestring);
        } else {
            ha_gaposa_command(&s_planner, (uint8_t)(ch - 1), c, now_ms());
            ha_mqtt_log("shade: ch%d <- %s", ch, jc->valuestring);
        }
        return true;
    }

    if (strcmp(op->valuestring, "shade_cal") == 0) {
        const cJSON *jch = cJSON_GetObjectItem(cmd, "ch");
        const cJSON *ju  = cJSON_GetObjectItem(cmd, "up_ms");
        const cJSON *jd  = cJSON_GetObjectItem(cmd, "down_ms");
        int ch = cJSON_IsNumber(jch) ? (int)jch->valuedouble : -1;
        if (ch < 1 || ch > SHADE_CHANNELS || !cJSON_IsNumber(ju) || !cJSON_IsNumber(jd)) {
            ha_mqtt_log("shade_cal: bad args");
            return true;
        }
        uint32_t up = (uint32_t)ju->valuedouble, dn = (uint32_t)jd->valuedouble;
        cal_store(ch - 1, up, dn);
        // Applied live as well as persisted, so a calibration run can iterate without a reboot.
        s_planner.cfg.travel_up_ms[ch - 1]   = up;
        s_planner.cfg.travel_down_ms[ch - 1] = dn;
        ha_mqtt_log("shade_cal: ch%d up=%ums down=%ums", ch, (unsigned)up, (unsigned)dn);
        return true;
    }

    return false;   // not ours — let ha_mqtt log it as unknown
}

// ── the control loop ─────────────────────────────────────────────────────────
// Planner decides, ha_dout guards, GPIO applies. Nothing here makes a policy decision — if this loop
// grows an `if` about shades, it belongs in ha_gaposa instead.
static void control_task(void *arg) {
    (void)arg;
    ha_gaposa_out_t out;
    for (;;) {
        uint32_t t = now_ms();
        ha_gaposa_tick(&s_planner, t, &out);

        for (int ch = 0; ch < SHADE_CHANNELS; ch++) {
            ha_gaposa_cmd_t a = out.assert_ch[ch];
            for (int fn = 0; fn < 3; fn++) {
                bool want = (fn == SHADE_UP && a == HA_GAPOSA_CMD_UP)
                         || (fn == SHADE_DW && a == HA_GAPOSA_CMD_DOWN)
                         // STOP and INTERIM both assert the St contact; the planner owns the difference
                         // in duration and batches them separately so they can never overlap.
                         || (fn == SHADE_ST && (a == HA_GAPOSA_CMD_STOP || a == HA_GAPOSA_CMD_INTERIM));

                ha_dout_t *d = &s_line[ch][fn];
                ha_dout_set(d, want, t);
                ha_dout_rc_t rc = ha_dout_tick(d, t);
                gpio_set_level(kPin[ch][fn], ha_dout_level(d) ? 1 : 0);

                if (rc == HA_DOUT_FAULTED && ha_dout_faulted(d)) {
                    // The independent cap fired: the planner asked for a contact longer than any legitimate
                    // command. Log loudly — this is the failure that would otherwise latch the panel.
                    ha_mqtt_log("shade: LINE WATCHDOG ch%d fn%d — forced off, latched", ch + 1, fn);
                    ha_dout_clear_fault(d, t);
                }
            }
        }
        vTaskDelay(pdMS_TO_TICKS(TICK_MS));
    }
}

// ── telemetry ────────────────────────────────────────────────────────────────
// Nodes are dumb relays (ADR-0001): publish what we know on the standard edge path and let the dictator
// own registry and area. Commanded and estimated are reported as SEPARATE fields with an explicit
// confidence, per ADR-0041 — an estimate is never shipped as though it were a measurement.
static const char *conf_str(ha_gaposa_conf_t c) {
    switch (c) {
    case HA_GAPOSA_POS_EXACT:     return "exact";
    case HA_GAPOSA_POS_ESTIMATED: return "estimated";
    default:                      return "unknown";
    }
}

static const char *cmd_str(ha_gaposa_cmd_t c) {
    switch (c) {
    case HA_GAPOSA_CMD_UP:      return "up";
    case HA_GAPOSA_CMD_DOWN:    return "down";
    case HA_GAPOSA_CMD_STOP:    return "stop";
    case HA_GAPOSA_CMD_INTERIM: return "interim";
    default:                    return "none";
    }
}

static void publish_channel(int ch) {
    ha_gaposa_conf_t conf;
    uint16_t pos = ha_gaposa_position(&s_planner, (uint8_t)ch, &conf);
    // Sized for the longest node_id ha_config accepts plus the suffix — the compiler checks this, and
    // a truncated registry key would silently split one shade into two device records.
    char key[24], reg[96], metrics[192];
    snprintf(key, sizeof key, "shade%d", ch + 1);
    snprintf(reg, sizeof reg, "%s-shade%d", s_cfg.node_id, ch + 1);
    snprintf(metrics, sizeof metrics,
             "{\"position_pct\":%.1f,\"position_confidence\":\"%s\",\"commanded\":\"%s\",\"moving\":%s}",
             pos / 10.0,
             conf_str(conf),
             cmd_str(ha_gaposa_commanded(&s_planner, (uint8_t)ch)),
             ha_gaposa_moving(&s_planner, (uint8_t)ch) ? "true" : "false");
    ha_mqtt_publish_node_sensor(key, reg, "shade", metrics);
}

static void telemetry_task(void *arg) {
    (void)arg;
    // Publish on change as well as on a heartbeat: a shade that just finished travelling should not wait
    // up to 30 s to say where it ended up.
    static uint16_t last_pos[HA_GAPOSA_MAX_CH];
    static ha_gaposa_conf_t last_conf[HA_GAPOSA_MAX_CH];
    static bool primed = false;
    uint32_t last_beat = 0;

    for (;;) {
        bool beat = !primed || (uint32_t)(now_ms() - last_beat) >= TELEMETRY_MS;
        for (int ch = 0; ch < SHADE_CHANNELS; ch++) {
            ha_gaposa_conf_t conf;
            uint16_t pos = ha_gaposa_position(&s_planner, (uint8_t)ch, &conf);
            bool changed = !primed || pos != last_pos[ch] || conf != last_conf[ch];
            if (beat || changed) {
                if (ha_mqtt_is_connected()) publish_channel(ch);
                last_pos[ch] = pos;
                last_conf[ch] = conf;
            }
        }
        if (beat) { last_beat = now_ms(); primed = true; }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

// ── boot ─────────────────────────────────────────────────────────────────────
static void gpio_init_safe(void) {
    for (int ch = 0; ch < SHADE_CHANNELS; ch++) {
        for (int fn = 0; fn < 3; fn++) {
            gpio_num_t p = kPin[ch][fn];
            gpio_reset_pin(p);
            // Latch the safe level BEFORE enabling the driver, so enabling output cannot emit a pulse.
            gpio_set_level(p, 0);
            gpio_set_direction(p, GPIO_MODE_OUTPUT);
            gpio_set_pull_mode(p, GPIO_PULLDOWN_ONLY);
            gpio_set_level(p, 0);
        }
    }
}

static bool repoint_healthy(void *user) { (void)user; return ha_mqtt_is_connected(); }

void app_main(void) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ESP_ERROR_CHECK(nvs_flash_init());
    }

    // Outputs safe before anything else can take time — this node's idle state is "no contact closed",
    // and every path that fails should land there.
    gpio_init_safe();

    // Darken the dev board's onboard RGB. This node uses no status LED — it lives inside the QCT
    // enclosure, where a lit LED is heat and nothing else — but a WS2812 LATCHES, so the colour left by
    // whatever shipped on the board stays lit forever at up to ~60 mA even though nothing drives it.
    //
    // Both candidates are blanked because the pin differs across S3 dev boards (GPIO48 on most, GPIO38 on
    // some DevKitC revisions) and ours is not confirmed. Blanking a pin with no LED on it just clocks
    // three bytes into nothing. Both are clear of the SPI flash (26–32), the octal PSRAM (33–37), the
    // strapping set (0/3/45/46) and all eighteen shade contacts above — see ADR-0041 §2.
    //
    // ⚠️ Do NOT add GPIO21 here: it is CH6's `Dw` contact on this board, and it is only the *Waveshare*
    // S3-ETH that has its WS2812 there (ha_led.c:15).
    for (int led_pin = 0; led_pin < 2; led_pin++) {
        static const int kCandidate[2] = { 48, 38 };
        ha_led_blank(kCandidate[led_pin]);
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

    // Planner up BEFORE the network, so a command arriving on the first connected millisecond has
    // somewhere to land. Travel times come from NVS; unset means position reports UNKNOWN, not zero.
    ha_gaposa_cfg_t gcfg = {
        .channels        = SHADE_CHANNELS,
        .pulse_ms        = 500,      // ⚠️ UNVERIFIED — the panel's minimum recognised width is
                                     // undocumented. Sweep from ~100 ms on the bench and set the real
                                     // value here (docs/design §3.3).
        .interim_hold_ms = 3000,     // Hold St ~3 s to recall the stored interim position. The GESTURE
                                     // (press-and-hold STOP) and the 3 s threshold are each documented —
                                     // XS30/40/50 programming guide and the Emitto Smart Line manual
                                     // respectively. ⚠️ What is still inferred is only that a maintained
                                     // St->com closure reproduces a handheld's held STOP. Verify on the
                                     // bench; see docs/design §4.
        .gap_ms          = 250,
    };
    cal_load(&gcfg);
    if (!ha_gaposa_init(&s_planner, &gcfg, now_ms())) {
        ESP_LOGE(TAG, "ha_gaposa_init refused the config — check pulse/interim against HA_GAPOSA_MAX_PULSE_MS");
        while (1) vTaskDelay(pdMS_TO_TICKS(10000));
    }

    // One ha_dout per physical line. active_high because the ULN2803 is a sinking INVERTING driver:
    // input HIGH pulls the QCT terminal down to `com`, which is the asserted state.
    for (int ch = 0; ch < SHADE_CHANNELS; ch++) {
        for (int fn = 0; fn < 3; fn++) {
            ha_dout_init(&s_line[ch][fn], &(ha_dout_cfg_t){
                .active_high = true,
                .max_on_ms   = LINE_MAX_ON_MS,   // independent of the planner, on purpose
            }, now_ms());
        }
    }

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
        .abilities = "shade", .enable_reach = false, .on_cmd = on_cmd });
    ha_mqtt_start(s_cfg.broker_uri, s_cfg.node_id);

    xTaskCreate(control_task,   "shade_ctl", 4096, NULL, 6, NULL);   // above telemetry: timing matters
    xTaskCreate(telemetry_task, "shade_tlm", 4096, NULL, 4, NULL);

    ESP_LOGI(TAG, "shade node up: node=%s broker=%s channels=%d", s_cfg.node_id, s_cfg.broker_uri,
             SHADE_CHANNELS);

    ha_ota_confirm_if_pending();
    ha_config_repoint_confirm(repoint_healthy, NULL, 60000);
}
