// XIAO ESP32-C6 HVAC node (ADR-0041) — Broan AI Series ERV over the D+/D- wall-control bus.
//
// Thin platform shim, by design (ADR-0020): `ha_broan` owns the token conversation, the heartbeat and
// the register cache; `ha_rs485` owns the half-duplex transport and the write gate. This file wires them
// to two GPIOs, MQTT and NVS, and makes no protocol decision of its own. If this file grows an `if`
// about Broan framing, it belongs in `ha_broan`.
//
// ⛔ THIS BUILD IS LISTEN-ONLY (bring-up step 1, ADR-0041 §1.12). It parks on the bus beside the working
// wall control and says NOTHING, which is what makes it safe to attach to a live ERV. Two independent
// gates enforce that — the transport refuses every write, and the state machine never produces one —
// and both are set from the single knob below. There is deliberately NO runtime override: taking this
// bus makes the ERV depend on us, and earning that should require a rebuild, not an MQTT message.
//
// Hardware (docs/design/hvac-shade-device-integration.md §"Node builds", verified on the bench
// 2026-09-22 against the board silk):
//
//   XIAO D10 (GPIO18) ──TX──> Waveshare TTL TO RS485 (C) RXD        3V3 -> VCC   (NOT 5V: C6 GPIOs
//   XIAO D9  (GPIO20) <─RX─── Waveshare TTL TO RS485 (C) TXD        GND -> GND    are not 5V tolerant)
//   Waveshare A+ -> Broan J9 D-    B- -> J9 D+    PE -> J9 GND      (note the A/B inversion)
//   XIAO D8  (GPIO19) ────────> OVR relay IN (active-high, 10k pull-down) — NEVER asserted in this build
//
// ⛔ RS-485 is on UART_NUM_1 and GPIO18/20 for a SAFETY reason, not an ergonomic one. UART0 is this
// tree's primary console (CONFIG_ESP_CONSOLE_UART_DEFAULT, USB-Serial-JTAG only secondary), so a
// transceiver on D6/D7 — or this transport configured on UART_NUM_0, which drags the console along with
// it — would dump the ROM-bootloader banner and the boot log onto a live ERV bus at every reset. The
// listen-only gates are application-layer and cannot stop the ROM bootloader. See ADR-0041.
//
// ⚠️ The transceiver is GALVANICALLY ISOLATED. XIAO GND and Broan GND must NOT be bonded — TTL-side GND
// serves the node, PE serves the ERV, and they never meet.
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

#include "ha_broan.h"
#include "ha_broan_frame.h"
#include "ha_config.h"
#include "ha_mqtt.h"
#include "ha_ota.h"
#include "ha_rs485.h"
#include "ha_sntp.h"
#include "ha_wifi.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#warning "secrets.h not found — copy secrets.example.h to secrets.h and fill it in (or provision NVS)."
#define HA_WIFI_SSID  ""
#define HA_WIFI_PSK   ""
#define HA_BROKER_URI "mqtt://192.168.1.200:1883"
#define HA_NODE_ID    "c6-hvac-bench"
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
#define HA_FW_VERSION "v1-hvac-listen"
#endif

// ⛔ THE GATE. 1 = sniff only, and that is the only value this node has ever been run with. Setting it
// to 0 means taking the wall control's slot on a live ventilator, which owes a heartbeat every 10 s or
// the unit raises E50 and SHUTS DOWN — including across every OTA. ADR-0041 §1.8 (does E50 self-clear,
// or need a physical power cycle?) is UNRESOLVED and BLOCKING for that step. Do not flip this to get
// telemetry: listen-only already harvests the full register map (see below).
#ifndef HA_HVAC_LISTEN_ONLY
#define HA_HVAC_LISTEN_ONLY 1
#endif

static const char *TAG = "ha_hvac";

// ── pin map ───────────────────────────────────────────────────────────────────
#define RS485_PORT       UART_NUM_1     // NOT UART_NUM_0 — see the header comment
#define RS485_TX_GPIO    GPIO_NUM_18    // silk D10 -> transceiver RXD
#define RS485_RX_GPIO    GPIO_NUM_20    // silk D9  <- transceiver TXD
#define RS485_DE_GPIO    (-1)           // auto-direction module (ha_rs485.h:24) — no DE/RE pin
#define BROAN_BAUD       38400u         // 38400 8N1, half-duplex (ha_broan_frame.h)

// Broan OVR failsafe relay. Active-high module (bench-verified: IN floating leaves NO-COM open, IN high
// shorts it), NO contact, 10k external pull-down. Held de-energized for the whole life of this build —
// OVR is a hard override that BEATS the serial bus, and a listen-only node has no business asserting it.
// When controller mode arrives this wants ha_dout (fail-safe state + max-on cap), not a bare gpio_set.
#define OVR_RELAY_GPIO   GPIO_NUM_19    // silk D8

#define BUS_READ_MS       50            // read timeout; also this task's idle tick
#define TELEMETRY_MS   30000
#define SNIFF_REPORT_MS 15000           // bring-up diagnostic cadence

static ha_rs485_t  s_bus;
static ha_broan_t  s_erv;
static ha_config_t s_cfg;

static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }

// ── JSON assembly ─────────────────────────────────────────────────────────────
// Every field is OMITTED unless the register has actually been seen. Two reasons, both load-bearing:
// a register that has never arrived must not be published as 0 (that is fabricated data in the system of
// record), and BROAN_REG_TEMP_EXHAUST reads NaN on units without the second thermistor — "nan" is not
// valid JSON and would poison the whole payload, not just that field.
typedef struct { char *buf; size_t cap; size_t off; bool any; } jb_t;

static void jb_init(jb_t *j, char *buf, size_t cap) {
    j->buf = buf; j->cap = cap; j->off = 0; j->any = false;
    if (cap) buf[0] = '\0';
}

static void jb_add(jb_t *j, const char *fmt, ...) {
    if (j->off >= j->cap) return;
    if (j->any && j->off + 1 < j->cap) j->buf[j->off++] = ',';
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(j->buf + j->off, j->cap - j->off, fmt, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= j->cap - j->off) { j->buf[j->off] = '\0'; return; }  // drop, never truncate
    j->off += (size_t)n;
    j->any = true;
}

static void jb_f32(jb_t *j, const char *name, uint16_t reg) {
    float v;
    if (!ha_broan_get_f32(&s_erv, reg, &v)) return;
    if (isnan(v) || isinf(v)) return;      // no 2nd thermistor, or a register we misread
    jb_add(j, "\"%s\":%.2f", name, v);
}

static void jb_i32(jb_t *j, const char *name, uint16_t reg) {
    int32_t v;
    if (!ha_broan_get_i32(&s_erv, reg, &v)) return;
    jb_add(j, "\"%s\":%ld", name, (long)v);
}

static const char *fan_mode_name(uint8_t m) {
    switch (m) {
    case BROAN_FAN_OFF:          return "off";
    case BROAN_FAN_OVR:          return "override";
    case BROAN_FAN_RECIRCULATE:  return "recirculate";
    case BROAN_FAN_INTERMITTENT: return "intermittent";
    case BROAN_FAN_MIN:          return "min";
    case BROAN_FAN_MAX:          return "max";
    case BROAN_FAN_MANUAL:       return "manual";
    case BROAN_FAN_TURBO:        return "turbo";
    case BROAN_FAN_HUMIDITY:     return "humidity";
    case BROAN_FAN_AWAY:         return "away";
    case BROAN_FAN_SMART:        return "smart";
    default:                     return "unknown";   // incl. the LCD's AUT/DEF, whose values are unknown
    }
}

// ── telemetry ─────────────────────────────────────────────────────────────────
// Listen-only still publishes real data: ha_broan caches register responses REGARDLESS of which
// controller they were addressed to, so parking beside the wall control validates the entire register
// map — wiring, polarity, baud, checksum and semantics — before we ever take the bus.
//
// ⚠️ Temperatures are published as `*_temp_raw` ON PURPOSE. Whether the ERV reports °C or °F is
// UNRESOLVED (ADR-0041 open question #6) and the component does no conversion. A field named
// `supply_temp` would be charted as °C by the first person to see it. Renaming these once §7.2 step 5
// settles the units is a deliberate one-time cost, taken while this node is still on the bench.
static void publish_erv(void) {
    char metrics[512], reg[96];
    jb_t j;
    jb_init(&j, metrics, sizeof metrics);

    uint8_t mode;
    if (ha_broan_get_u8(&s_erv, BROAN_REG_FAN_MODE, &mode)) {
        jb_add(&j, "\"fan_mode\":%u", (unsigned)mode);
        jb_add(&j, "\"fan_mode_name\":\"%s\"", fan_mode_name(mode));
    }
    jb_i32(&j, "active_mode",     BROAN_REG_ACTIVE_MODE);
    jb_f32(&j, "power_w",         BROAN_REG_POWER_W);
    jb_f32(&j, "supply_temp_raw", BROAN_REG_TEMP_SUPPLY);
    jb_f32(&j, "exhaust_temp_raw", BROAN_REG_TEMP_EXHAUST);
    jb_f32(&j, "supply_cfm",      BROAN_REG_CFM_SUPPLY);
    jb_f32(&j, "exhaust_cfm",     BROAN_REG_CFM_EXHAUST);
    jb_f32(&j, "supply_rpm",      BROAN_REG_RPM_SUPPLY);
    jb_f32(&j, "exhaust_rpm",     BROAN_REG_RPM_EXHAUST);
    jb_i32(&j, "filter_life_s",   BROAN_REG_FILTER_LIFE);
    jb_i32(&j, "uptime_s",        BROAN_REG_UPTIME);

    // Fault/warning read -1 when healthy. Publish the raw code AND the interpretation, so a dashboard
    // never has to know that -1 is the good value.
    int32_t code;
    if (ha_broan_get_i32(&s_erv, BROAN_REG_FAULT, &code)) {
        jb_add(&j, "\"fault_code\":%ld", (long)code);
        jb_add(&j, "\"fault_ok\":%s", broan_code_is_ok(code) ? "true" : "false");
    }
    if (ha_broan_get_i32(&s_erv, BROAN_REG_WARNING, &code)) {
        jb_add(&j, "\"warning_code\":%ld", (long)code);
        jb_add(&j, "\"warning_ok\":%s", broan_code_is_ok(code) ? "true" : "false");
    }

    jb_add(&j, "\"online\":%s", ha_broan_online(&s_erv, now_ms()) ? "true" : "false");
    // Published so a consumer can tell harvested-from-the-wall-control data apart from data we polled
    // for ourselves, without having to know which firmware is on the node.
    jb_add(&j, "\"listen_only\":%s", ha_rs485_is_listen_only(&s_bus) ? "true" : "false");

    if (!j.any) return;    // nothing harvested yet — publishing an empty object would read as a live zero

    snprintf(reg, sizeof reg, "%s-erv", s_cfg.node_id);
    ha_mqtt_publish_node_sensor("erv", reg, "erv", metrics);
}

// ── bring-up diagnostic ───────────────────────────────────────────────────────
// The two ways this wiring goes wrong are indistinguishable from "the ERV is quiet" unless you look at
// the byte counters, so the node interprets them itself rather than leaving it to whoever is at the
// bench. This is the automated form of the one-wire swap test in the design doc.
static void sniff_report(void) {
    ha_rs485_stats_t t;
    ha_broan_stats_t b;
    ha_rs485_get_stats(&s_bus, &t);
    ha_broan_get_stats(&s_erv, &b);

    const char *verdict;
    if (t.rx_bytes == 0) {
        verdict = "NO BYTES AT ALL — the C6 is not hearing the bus. Check the transceiver's data-out is "
                  "on D9/GPIO20 (the wiki's RXD/TXD labels are ambiguous — try the other TTL pad), then "
                  "that A+/B- are landed on J9 and PE on J9 GND.";
    } else if (b.frames_rx == 0) {
        verdict = "BYTES BUT ZERO VALID FRAMES — almost certainly A/B swapped (J9 D+ goes to B-, D- goes "
                  "to A+), or the baud is wrong. Swapping A/B is non-destructive.";
    } else if (b.frames_bad > b.frames_rx / 4) {
        verdict = "FRAMES DECODING, BUT A HIGH REJECT RATE — suspect a marginal bus (did a third "
                  "termination resistor get added?) or a noisy ground reference.";
    } else {
        verdict = "OK";
    }

    ha_mqtt_log("sniff: rx=%llu B frames=%lu bad=%lu pings=%lu tokens=%lu | writes_refused=%lu | %s",
                (unsigned long long)t.rx_bytes, (unsigned long)b.frames_rx, (unsigned long)b.frames_bad,
                (unsigned long)b.pings, (unsigned long)b.tokens, (unsigned long)t.writes_refused,
                verdict);
    ESP_LOGI(TAG, "sniff: rx=%llu B frames=%lu bad=%lu | %s",
             (unsigned long long)t.rx_bytes, (unsigned long)b.frames_rx, (unsigned long)b.frames_bad,
             verdict);
}

// ── the bus loop ──────────────────────────────────────────────────────────────
// Bytes in, bytes out. ha_broan decides everything; this only moves octets.
static void bus_task(void *arg) {
    (void)arg;
    uint8_t rx[256];
    uint8_t tx[BROAN_MAX_FRAME];

    for (;;) {
        int n = ha_rs485_read(&s_bus, rx, sizeof rx, BUS_READ_MS);
        uint32_t t = now_ms();
        if (n > 0) ha_broan_rx(&s_erv, rx, (size_t)n, t);

        size_t len = ha_broan_next_tx(&s_erv, tx, sizeof tx, t);
        if (len == 0) continue;

        // ha_broan_next_tx() is contracted to return 0 whenever listen_only is set. Reaching here with
        // bytes in hand means the component's own gate has failed — so this build drops the frame and
        // says so, rather than handing it to a transport whose gate would silently swallow it. A
        // listen-only node that quietly starts transmitting is the failure worth being loud about.
        if (ha_rs485_is_listen_only(&s_bus)) {
            ESP_LOGE(TAG, "GATE VIOLATION: ha_broan produced %u tx bytes while listen_only — DROPPED",
                     (unsigned)len);
            ha_mqtt_log("hvac: GATE VIOLATION — ha_broan produced %u tx bytes under listen_only, dropped",
                        (unsigned)len);
            continue;
        }

        esp_err_t err = ha_rs485_write(&s_bus, tx, len, 200);
        if (err != ESP_OK) ha_mqtt_log("hvac: tx failed (%s)", esp_err_to_name(err));
    }
}

static void telemetry_task(void *arg) {
    (void)arg;
    uint32_t last_pub = 0, last_sniff = 0;
    bool primed = false;

    for (;;) {
        uint32_t t = now_ms();
        if (!primed || (uint32_t)(t - last_pub) >= TELEMETRY_MS) {
            if (ha_mqtt_is_connected()) publish_erv();
            last_pub = t;
            primed = true;
        }
        // The sniff report runs for the whole life of a listen-only build: this node exists to answer
        // "is the wiring right", and that question stays live until the bus is taken.
        if ((uint32_t)(t - last_sniff) >= SNIFF_REPORT_MS) {
            if (ha_mqtt_is_connected()) sniff_report();
            last_sniff = t;
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

// ── commands ──────────────────────────────────────────────────────────────────
// Read-only by construction. There is no command to clear listen_only, and there must not be — see the
// gate comment at the top. `erv_stats` just forces an immediate report instead of waiting for the tick.
static bool on_cmd(const cJSON *cmd, void *user) {
    (void)user;
    const cJSON *op = cJSON_GetObjectItem(cmd, "op");
    if (!cJSON_IsString(op)) return false;

    if (strcmp(op->valuestring, "erv_stats") == 0) {
        sniff_report();
        publish_erv();
        return true;
    }
    return false;   // not ours — let ha_mqtt log it as unknown
}

// ── boot ──────────────────────────────────────────────────────────────────────
// Relay de-energized before anything else can take time. ADR-0041: the pin floats as an input from reset
// until this runs, and on an active-high module a stray high asserts a hard override that beats the
// serial bus. The external 10k pull-down covers the window before this line; this covers everything
// after. Latch the level BEFORE enabling the driver so enabling output cannot emit a pulse.
static void relay_init_safe(void) {
    gpio_reset_pin(OVR_RELAY_GPIO);
    gpio_set_level(OVR_RELAY_GPIO, 0);
    gpio_set_direction(OVR_RELAY_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_pull_mode(OVR_RELAY_GPIO, GPIO_PULLDOWN_ONLY);
    gpio_set_level(OVR_RELAY_GPIO, 0);
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

#ifdef HA_HVAC_EXT_ANTENNA
    // XIAO C6 RF path select — GPIO3 LOW + GPIO14 HIGH routes RF to the U.FL connector (ADR-0041).
    // ⚠️ Only build with this defined when an external antenna is ACTUALLY ATTACHED. Switching to an
    // unconnected U.FL is far worse than the ceramic antenna, and presents as a placement problem.
    gpio_reset_pin(GPIO_NUM_3);
    gpio_set_direction(GPIO_NUM_3, GPIO_MODE_OUTPUT);
    gpio_set_level(GPIO_NUM_3, 0);
    gpio_reset_pin(GPIO_NUM_14);
    gpio_set_direction(GPIO_NUM_14, GPIO_MODE_OUTPUT);
    gpio_set_level(GPIO_NUM_14, 1);
#endif

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

    // Transport up before the network: the ERV is talking to its wall control right now, and there is no
    // reason to miss the first seconds of it while Wi-Fi associates.
    ha_rs485_cfg_t bcfg = {
        .port        = RS485_PORT,
        .tx_gpio     = RS485_TX_GPIO,
        .rx_gpio     = RS485_RX_GPIO,
        .de_gpio     = RS485_DE_GPIO,
        .baud        = BROAN_BAUD,
        .data_bits   = UART_DATA_8_BITS,
        .parity      = UART_PARITY_DISABLE,
        .stop_bits   = UART_STOP_BITS_1,
        .listen_only = HA_HVAC_LISTEN_ONLY,
        // Left false deliberately (ha_rs485.h:50): enable only if THIS board is observed looping TX back
        // into RX. It cannot be observed at all until we transmit, which listen-only never does.
        .discard_echo = false,
    };
    if (ha_rs485_init(&s_bus, &bcfg) != ESP_OK) {
        ESP_LOGE(TAG, "ha_rs485_init failed — check tx/rx gpio and port");
        while (1) vTaskDelay(pdMS_TO_TICKS(10000));
    }

    // Default poll list (mode, power, temps, CFM, RPM, filter, fault, warning, uptime). It is not used
    // while listen-only — we never send a read request — but the field cache fills anyway from the wall
    // control's own traffic, which is the whole point of this step.
    ha_broan_init(&s_erv, &(ha_broan_cfg_t){ .listen_only = HA_HVAC_LISTEN_ONLY }, now_ms());

    // Bus task before Wi-Fi for the same reason as the transport: start listening immediately.
    xTaskCreate(bus_task, "erv_bus", 4096, NULL, 6, NULL);

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
        .abilities = "erv", .enable_reach = false, .on_cmd = on_cmd });
    ha_mqtt_start(s_cfg.broker_uri, s_cfg.node_id);

    xTaskCreate(telemetry_task, "erv_tlm", 4096, NULL, 4, NULL);

    ESP_LOGW(TAG, "hvac node up: node=%s broker=%s uart=%d tx=%d rx=%d baud=%u LISTEN_ONLY=%d",
             s_cfg.node_id, s_cfg.broker_uri, RS485_PORT, RS485_TX_GPIO, RS485_RX_GPIO,
             (unsigned)BROAN_BAUD, HA_HVAC_LISTEN_ONLY);
#if HA_HVAC_LISTEN_ONLY
    ESP_LOGW(TAG, "LISTEN-ONLY: this node will never transmit. OVR relay (GPIO%d) held de-energized.",
             OVR_RELAY_GPIO);
#else
    ESP_LOGE(TAG, "⛔ LISTEN_ONLY IS CLEARED — this node will TAKE THE BUS and owes the ERV a heartbeat "
                  "every %u ms or it raises E50 and shuts down. ADR-0041 §1.8.", BROAN_HEARTBEAT_MS);
#endif

    ha_ota_confirm_if_pending();
    ha_config_repoint_confirm(repoint_healthy, NULL, 60000);
}
