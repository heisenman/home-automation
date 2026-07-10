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

static const char *TAG = "ha_gas";

// XIAO ESP32-C6 / S3-ETH default I2C pads: D4 = GPIO22 (SDA), D5 = GPIO23 (SCL). Grove SGPxx carries its own
// 10k pull-ups; internal pull-ups enabled too as belt-and-braces.
#define GAS_SDA_GPIO   22
#define GAS_SCL_GPIO   23
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

// One-shot I2C bus scan — the raw ACK proof, independent of the driver. Logs every 7-bit address that ACKs
// so a mis-solder (nothing / wrong pins) is instantly distinguishable from a driver-level fault.
static void gas_bus_scan(i2c_master_bus_handle_t bus) {
    char found[80]; int n = 0; found[0] = '\0';
    for (uint8_t addr = 0x08; addr <= 0x77; addr++) {
        if (i2c_master_probe(bus, addr, 50) == ESP_OK)
            n += snprintf(found + n, sizeof(found) - n, "%s0x%02X", n ? "," : "", addr);
        if (n >= (int)sizeof(found) - 6) break;
    }
    if (found[0]) ha_mqtt_log("gas bus scan (SDA=GPIO%d SCL=GPIO%d): ACK %s", GAS_SDA_GPIO, GAS_SCL_GPIO, found);
    else          ha_mqtt_log("gas bus scan (SDA=GPIO%d SCL=GPIO%d): NO devices ACK — check wiring/power",
                              GAS_SDA_GPIO, GAS_SCL_GPIO);
}

static void gas_task(void *arg) {
    (void)arg;
    int since_pub = s_publish_every;        // publish the first sample promptly
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(s_sample_ms));
        char metrics[128];
        if (s_sensor == HA_GAS_SENSOR_SGP30) {
            uint16_t eco2 = 0, tvoc = 0;    // first ~15 reads after init are the fixed 400 ppm / 0 ppb warmup
            esp_err_t e = sgp30_measure(&s_sgp30, &eco2, &tvoc);
            if (e != ESP_OK) { ESP_LOGW(TAG, "sgp30_measure: %s", esp_err_to_name(e)); continue; }
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

void ha_gas_start(const char *node_id, ha_gas_sensor_t sensor) {
    s_sensor = sensor;
    snprintf(s_reg_key, sizeof(s_reg_key), "%s-gas", node_id ? node_id : "unknown");
    if (sensor == HA_GAS_SENSOR_SGP30)       { s_dev_type = "sgp30_gas";  s_sample_ms = 1000;  s_publish_every = 10; }
    else if (sensor == HA_GAS_SENSOR_BME680) { s_dev_type = "bme680_gas"; s_sample_ms = 10000; s_publish_every = 1; }
    else                              { s_dev_type = "sgp40_gas";  s_sample_ms = 1000;  s_publish_every = 10; }

    i2c_master_bus_config_t bus_cfg = {
        .clk_source                   = I2C_CLK_SRC_DEFAULT,
        .i2c_port                     = GAS_I2C_PORT,
        .sda_io_num                   = GAS_SDA_GPIO,
        .scl_io_num                   = GAS_SCL_GPIO,
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
                    GAS_SDA_GPIO, GAS_SCL_GPIO);
    } else if (sensor == HA_GAS_SENSOR_BME680) {
        // Try addr 0x76 (SDO->GND) then 0x77 (SDO->VCC); init verifies chip id 0x61 = the live wiring check.
        if ((e = bme680_init(&s_bme, bus, BME680_I2C_ADDR_PRIMARY, GAS_SCL_HZ)) != ESP_OK &&
            (e = bme680_init(&s_bme, bus, BME680_I2C_ADDR_SECONDARY, GAS_SCL_HZ)) != ESP_OK) {
            ha_mqtt_log("BME680 NOT ready (init: %s) — check D4=SDA / D5=SCL / 3V3 / GND / addr 0x76|0x77; "
                        "BLE relay continues", esp_err_to_name(e));
            return;
        }
        ha_mqtt_log("BME680 up — T/RH/P + gas-Ω lane running (SDA=GPIO%d SCL=GPIO%d, ~10s cadence, "
                    "heater 320C/150ms, compensation baked in)", GAS_SDA_GPIO, GAS_SCL_GPIO);
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
                    GAS_SDA_GPIO, GAS_SCL_GPIO);
        GasIndexAlgorithm_init(&s_voc, GasIndexAlgorithm_ALGORITHM_TYPE_VOC);
    }
    xTaskCreate(gas_task, "gas", 4096, NULL, 4, NULL);
}
