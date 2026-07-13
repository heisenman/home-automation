#include "ha_gas.h"
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "driver/i2c_master.h"
#include "sgp40.h"
#include "sensirion_gas_index_algorithm.h"
#include "sgp30.h"
#include "bme680.h"
#include "ha_mqtt.h"
#include "nvs.h"
#include <time.h>
#include <stdbool.h>

static const char *TAG = "ha_gas";

// I2C pads are BOARD-SPECIFIC and passed into ha_gas_start (was a per-board compile-time #define before the
// ADR-0020 unify; flattening it to one default silently bricked the S3 — see below). Known-good pads:
//   XIAO ESP32-C6           : D4 = GPIO22 (SDA), D5 = GPIO23 (SCL)
//   Waveshare ESP32-S3-ETH  : GPIO42 (SDA), GPIO41 (SCL)  — GPIO22/23 DO NOT EXIST on the S3 (i2c bus init
//                             fails ESP_ERR_INVALID_ARG), and these pads clear the W5500 SPI (9-14) + LED (21).
// Grove SGPxx carries its own 10k pull-ups; internal pull-ups enabled too as belt-and-braces.
#define GAS_SCL_HZ     400000
#define GAS_I2C_PORT   I2C_NUM_0
#define GAS_TOPIC_KEY  "gas"

// Per-sensor state — only the selected one is used (all compiled in; the drivers are small).
static sgp40_t s_sgp40;
static GasIndexAlgorithmParams s_voc;
static sgp30_t s_sgp30;
static bme680_t s_bme;

// Runtime config resolved in ha_gas_start (was compile-time #ifdef in the old per-node forks).
static ha_gas_sensor_t s_sensor;
static char s_reg_key[48];              // registry key / payload "mac" = "<node_id>-gas"
static const char *s_dev_type;
static int s_sample_ms;                 // Sensirion algos REQUIRE 1 Hz; BME680 forced-mode ~10 s
static int s_publish_every;             // publish 1 in N samples
static int s_sda, s_scl;                // board-specific I2C pads, set from ha_gas_start args

// One-shot I2C bus scan — the raw ACK proof, independent of the driver. Logs every 7-bit address that ACKs
// so a mis-solder (nothing / wrong pins) is instantly distinguishable from a driver-level fault.
static void gas_bus_scan(i2c_master_bus_handle_t bus) {
    char found[80]; int n = 0; found[0] = '\0';
    for (uint8_t addr = 0x08; addr <= 0x77; addr++) {
        if (i2c_master_probe(bus, addr, 50) == ESP_OK)
            n += snprintf(found + n, sizeof(found) - n, "%s0x%02X", n ? "," : "", addr);
        if (n >= (int)sizeof(found) - 6) break;
    }
    if (found[0]) ha_mqtt_log("gas bus scan (SDA=GPIO%d SCL=GPIO%d): ACK %s", s_sda, s_scl, found);
    else          ha_mqtt_log("gas bus scan (SDA=GPIO%d SCL=GPIO%d): NO devices ACK — check wiring/power",
                              s_sda, s_scl);
}

// --- SGP30 dynamic-baseline persistence (ADR-0035 Q3 — drift root-cause) --------------------------------
// The SGP30's on-chip eCO2/TVOC baseline takes ~12 h to settle and is LOST on every reboot, so a cold node
// re-learns from the fixed 400/0 warmup for half a day — the "baseline creep" the air-quality
// characterization measured (daily-mean TVOC swinging 43->292->124 ppb, 525 just-reset samples). Persist the
// live baseline to NVS hourly and restore it on boot so reboots resume the settled algorithm state instead
// of re-learning. Sensirion rule: only restore a value stored within the last week, else discard it.
#define SGP30_NVS_NS      "sgp30"
#define SGP30_NVS_KEY     "base"
#define SGP30_BASE_MAGIC  0x53475033u            // 'SGP3' — guards against a stale/foreign blob
#define SGP30_SAVE_PERIOD_S  3600                // persist the live baseline hourly (Sensirion cadence)
#define SGP30_SETTLE_S       (12 * 3600)         // don't persist until the ~12 h settle has elapsed
#define SGP30_MAX_AGE_S      (7 * 24 * 3600)     // discard a stored baseline older than a week (Sensirion)

typedef struct {
    uint32_t magic;
    uint16_t eco2_base;
    uint16_t tvoc_base;
    int64_t  saved_epoch;                        // wall-clock (s) at save; <=0 means the clock wasn't set
} sgp30_baseline_store_t;

static bool sgp30_clock_set(void) { return time(NULL) > 1700000000; }   // ~2023-11 sanity floor

// Restore a stored baseline into the live sensor, if present and fresh. Returns true iff applied.
static bool sgp30_baseline_restore(sgp30_t *s) {
    nvs_handle_t h;
    if (nvs_open(SGP30_NVS_NS, NVS_READONLY, &h) != ESP_OK) return false;   // namespace never written
    sgp30_baseline_store_t st;
    size_t len = sizeof(st);
    esp_err_t e = nvs_get_blob(h, SGP30_NVS_KEY, &st, &len);
    nvs_close(h);
    if (e != ESP_OK || len != sizeof(st) || st.magic != SGP30_BASE_MAGIC) return false;
    int64_t age = (int64_t)time(NULL) - st.saved_epoch;
    if (st.saved_epoch <= 0 || age < 0 || age > SGP30_MAX_AGE_S) {
        ha_mqtt_log("SGP30 baseline in NVS stale/unclocked (age=%llds) — re-learning fresh", (long long)age);
        return false;
    }
    if (sgp30_set_baseline(s, st.eco2_base, st.tvoc_base) != ESP_OK) return false;
    ha_mqtt_log("SGP30 baseline restored from NVS (eco2=0x%04X tvoc=0x%04X, age=%lldh) — skipped ~12h re-learn",
                st.eco2_base, st.tvoc_base, (long long)(age / 3600));
    return true;
}

// Read the live baseline and persist it with a timestamp.
static void sgp30_baseline_save(sgp30_t *s) {
    uint16_t eco2b = 0, tvocb = 0;
    if (sgp30_get_baseline(s, &eco2b, &tvocb) != ESP_OK) return;
    sgp30_baseline_store_t st = { .magic = SGP30_BASE_MAGIC, .eco2_base = eco2b,
                                  .tvoc_base = tvocb, .saved_epoch = (int64_t)time(NULL) };
    nvs_handle_t h;
    if (nvs_open(SGP30_NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    esp_err_t e = nvs_set_blob(h, SGP30_NVS_KEY, &st, sizeof(st));
    if (e == ESP_OK) e = nvs_commit(h);
    nvs_close(h);
    if (e == ESP_OK) ha_mqtt_log("SGP30 baseline persisted to NVS (eco2=0x%04X tvoc=0x%04X)", eco2b, tvocb);
}

static void gas_task(void *arg) {
    (void)arg;
    int since_pub = s_publish_every;        // publish the first sample promptly
    int64_t sgp30_secs = 0;                 // SGP30 runtime seconds (1 Hz loop) for settle/save timing
    int64_t sgp30_next_save_s = SGP30_SETTLE_S;   // first persist only after the ~12 h settle window
    bool sgp30_restore_tried = false;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(s_sample_ms));
        char metrics[128];
        if (s_sensor == HA_GAS_SENSOR_SGP30) {
            // Restore the settled baseline once, as soon as the clock is trustworthy (app_main blocks on
            // SNTP before us, but guard its 15 s-timeout miss). Give up after ~2 min so a clockless node
            // still measures — it just re-learns from scratch.
            if (!sgp30_restore_tried && (sgp30_clock_set() || sgp30_secs > 120)) {
                sgp30_baseline_restore(&s_sgp30);
                sgp30_restore_tried = true;
            }
            uint16_t eco2 = 0, tvoc = 0;    // first ~15 reads after init are the fixed 400 ppm / 0 ppb warmup
            esp_err_t e = sgp30_measure(&s_sgp30, &eco2, &tvoc);
            if (e != ESP_OK) { ESP_LOGW(TAG, "sgp30_measure: %s", esp_err_to_name(e)); continue; }
            sgp30_secs += s_sample_ms / 1000;
            // Persist the live baseline hourly, but only past the ~12 h settle and with a set clock (so the
            // stored timestamp — hence the on-restore freshness check — is meaningful).
            if (sgp30_secs >= sgp30_next_save_s && sgp30_clock_set()) {
                sgp30_baseline_save(&s_sgp30);
                sgp30_next_save_s = sgp30_secs + SGP30_SAVE_PERIOD_S;
            }
            if (++since_pub < s_publish_every) continue;
            snprintf(metrics, sizeof(metrics), "{\"eco2\":%u,\"tvoc\":%u}", (unsigned)eco2, (unsigned)tvoc);
        } else if (s_sensor == HA_GAS_SENSOR_BME680) {
            // Forced-mode T/RH/P + gas-Ω; the driver already applied the Bosch compensation (physical units).
            bme680_reading_t r;
            esp_err_t e = bme680_measure(&s_bme, &r);
            if (e != ESP_OK) { ESP_LOGW(TAG, "bme680_measure: %s", esp_err_to_name(e)); continue; }
            if (++since_pub < s_publish_every) continue;
            snprintf(metrics, sizeof(metrics),
                     "{\"temperature_c\":%.2f,\"humidity_pct\":%.1f,\"pressure_hpa\":%.1f,"
                     "\"gas_ohm\":%.0f,\"gas_valid\":%u}",
                     r.temperature_c, r.humidity_pct, r.pressure_hpa, r.gas_resistance_ohm,
                     (unsigned)r.gas_valid);
        } else {  // HA_GAS_SENSOR_SGP40
            uint16_t sraw = 0;
            esp_err_t e = sgp40_measure_raw(&s_sgp40, SGP40_DEFAULT_RH, SGP40_DEFAULT_T, &sraw);
            if (e != ESP_OK) { ESP_LOGW(TAG, "measure_raw: %s", esp_err_to_name(e)); continue; }
            int32_t voc = 0;                // 0 during the first ~45 s blackout, then 1..500 (100 = baseline)
            GasIndexAlgorithm_process(&s_voc, (int32_t)sraw, &voc);
            if (++since_pub < s_publish_every) continue;
            snprintf(metrics, sizeof(metrics), "{\"voc_index\":%ld,\"voc_raw\":%u}", (long)voc, (unsigned)sraw);
        }
        since_pub = 0;
        ha_mqtt_publish_node_sensor(GAS_TOPIC_KEY, s_reg_key, s_dev_type, metrics);
    }
}

void ha_gas_start(const char *node_id, ha_gas_sensor_t sensor, int sda_gpio, int scl_gpio) {
    s_sensor = sensor;
    s_sda = sda_gpio;
    s_scl = scl_gpio;
    snprintf(s_reg_key, sizeof(s_reg_key), "%s-gas", node_id ? node_id : "unknown");
    if (sensor == HA_GAS_SENSOR_SGP30)       { s_dev_type = "sgp30_gas";  s_sample_ms = 1000;  s_publish_every = 10; }
    else if (sensor == HA_GAS_SENSOR_BME680) { s_dev_type = "bme680_gas"; s_sample_ms = 10000; s_publish_every = 1; }
    else                              { s_dev_type = "sgp40_gas";  s_sample_ms = 1000;  s_publish_every = 10; }

    i2c_master_bus_config_t bus_cfg = {
        .clk_source                   = I2C_CLK_SRC_DEFAULT,
        .i2c_port                     = GAS_I2C_PORT,
        .sda_io_num                   = s_sda,
        .scl_io_num                   = s_scl,
        .glitch_ignore_cnt            = 7,
        .flags.enable_internal_pullup = true,
    };
    i2c_master_bus_handle_t bus;
    esp_err_t e = i2c_new_master_bus(&bus_cfg, &bus);
    if (e != ESP_OK) { ha_mqtt_log("GAS: I2C bus init failed: %s", esp_err_to_name(e)); return; }
    gas_bus_scan(bus);   // raw ACK proof before we trust the driver

    if (sensor == HA_GAS_SENSOR_SGP30) {
        // sgp30_init = add device + Init_air_quality; the Init transmit is the live wiring/presence check.
        if ((e = sgp30_init(&s_sgp30, bus, GAS_SCL_HZ)) != ESP_OK) {
            ha_mqtt_log("SGP30 NOT ready (init: %s) — check D4=SDA / D5=SCL / 3V3 / GND; BLE relay continues",
                        esp_err_to_name(e));
            return;
        }
        ha_mqtt_log("SGP30 up — eCO2/TVOC lane running (SDA=GPIO%d SCL=GPIO%d, 1Hz, ~15s warmup at 400/0)",
                    s_sda, s_scl);
    } else if (sensor == HA_GAS_SENSOR_BME680) {
        // Try addr 0x76 (SDO->GND) then 0x77 (SDO->VCC); init verifies chip id 0x61 = the live wiring check.
        if ((e = bme680_init(&s_bme, bus, BME680_I2C_ADDR_PRIMARY, GAS_SCL_HZ)) != ESP_OK &&
            (e = bme680_init(&s_bme, bus, BME680_I2C_ADDR_SECONDARY, GAS_SCL_HZ)) != ESP_OK) {
            ha_mqtt_log("BME680 NOT ready (init: %s) — check D4=SDA / D5=SCL / 3V3 / GND / addr 0x76|0x77; "
                        "BLE relay continues", esp_err_to_name(e));
            return;
        }
        ha_mqtt_log("BME680 up — T/RH/P + gas-Ω lane running (SDA=GPIO%d SCL=GPIO%d, ~10s cadence, "
                    "heater 320C/150ms, compensation baked in)", s_sda, s_scl);
    } else {  // HA_GAS_SENSOR_SGP40
        if ((e = sgp40_init(&s_sgp40, bus, GAS_SCL_HZ)) != ESP_OK) {
            ha_mqtt_log("SGP40: add-device failed: %s", esp_err_to_name(e)); return;
        }
        // Self-test is also the live wiring check (fails if SDA/SCL/VCC aren't connected).
        if ((e = sgp40_self_test(&s_sgp40)) != ESP_OK) {
            ha_mqtt_log("SGP40 NOT ready (self-test: %s) — check D4=SDA / D5=SCL / 3V3 / GND; BLE relay continues",
                        esp_err_to_name(e));
            return;
        }
        ha_mqtt_log("SGP40 self-test PASS — VOC lane up (SDA=GPIO%d SCL=GPIO%d, 1Hz, ~45s warmup)",
                    s_sda, s_scl);
        GasIndexAlgorithm_init(&s_voc, GasIndexAlgorithm_ALGORITHM_TYPE_VOC);
    }
    xTaskCreate(gas_task, "gas", 4096, NULL, 4, NULL);
}
