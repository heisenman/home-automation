#include "ha_gas.h"
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "driver/i2c_master.h"

#if __has_include("secrets.h")
#include "secrets.h"                         // HA_NODE_ID, and optionally HA_GAS_SGP30 (sensor select)
#endif
#ifndef HA_NODE_ID
#define HA_NODE_ID "unknown"
#endif

// Sensor select (compile-time): define HA_GAS_SGP30 in secrets.h for a Sensirion SGP30 (eCO2 + TVOC, I2C
// 0x58); the default is the SGP40 (VOC index, 0x59). Both are modular shared components (ADR-0020) — this
// file is only the node glue that picks one, reads it at 1 Hz, and publishes node-local gas readings.
#if defined(HA_GAS_SGP30)
  #include "sgp30.h"
  #define GAS_DEV_TYPE "sgp30_gas"
#else
  #include "sgp40.h"
  #include "sensirion_gas_index_algorithm.h"
  #define GAS_DEV_TYPE "sgp40_gas"
#endif
#include "ha_mqtt.h"

static const char *TAG = "ha_gas";

// XIAO ESP32-C6 default I2C pads: D4 = GPIO22 (SDA), D5 = GPIO23 (SCL). Grove SGPxx carries its own
// 10k pull-ups; internal pull-ups enabled too as belt-and-braces.
#define GAS_SDA_GPIO      22
#define GAS_SCL_GPIO      23
#define GAS_SCL_HZ        400000
#define GAS_I2C_PORT      I2C_NUM_0

#define GAS_SAMPLE_MS     1000     // Sensirion algorithms REQUIRE a fixed 1 Hz cadence
#define GAS_PUBLISH_EVERY 10       // publish 1 in 10 samples -> a reading every ~10 s

// Registry key (payload "mac" — resolved by edge_mapper against instance/devices.yaml) + topic segment.
// NOT a BLE MAC; the mapper does a plain dict lookup. Derived from the node id so each gas node is
// distinct (coffice_c6 -> "coffice_c6-gas", cbed_c6 -> "cbed_c6-gas").
#define GAS_TOPIC_KEY  "gas"
#define GAS_REG_KEY    HA_NODE_ID "-gas"

#if defined(HA_GAS_SGP30)
static sgp30_t s_sgp;
#else
static sgp40_t s_sgp;
static GasIndexAlgorithmParams s_voc;
#endif

static void gas_task(void *arg) {
    (void)arg;
    int since_pub = GAS_PUBLISH_EVERY;     // publish the first sample promptly
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(GAS_SAMPLE_MS));
        char metrics[64];
#if defined(HA_GAS_SGP30)
        uint16_t eco2 = 0, tvoc = 0;       // first ~15 reads after init are the fixed 400 ppm / 0 ppb warmup
        esp_err_t e = sgp30_measure(&s_sgp, &eco2, &tvoc);
        if (e != ESP_OK) { ESP_LOGW(TAG, "sgp30_measure: %s", esp_err_to_name(e)); continue; }
        if (++since_pub < GAS_PUBLISH_EVERY) continue;
        snprintf(metrics, sizeof(metrics), "{\"eco2\":%u,\"tvoc\":%u}", (unsigned)eco2, (unsigned)tvoc);
#else
        uint16_t sraw = 0;
        esp_err_t e = sgp40_measure_raw(&s_sgp, SGP40_DEFAULT_RH, SGP40_DEFAULT_T, &sraw);
        if (e != ESP_OK) { ESP_LOGW(TAG, "measure_raw: %s", esp_err_to_name(e)); continue; }
        int32_t voc = 0;                    // 0 during the first ~45 s blackout, then 1..500 (100 = baseline)
        GasIndexAlgorithm_process(&s_voc, (int32_t)sraw, &voc);
        if (++since_pub < GAS_PUBLISH_EVERY) continue;
        snprintf(metrics, sizeof(metrics), "{\"voc_index\":%ld,\"voc_raw\":%u}", (long)voc, (unsigned)sraw);
#endif
        since_pub = 0;
        ha_mqtt_publish_node_sensor(GAS_TOPIC_KEY, GAS_REG_KEY, GAS_DEV_TYPE, metrics);
    }
}

void ha_gas_start(void) {
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

#if defined(HA_GAS_SGP30)
    // sgp30_init = add device + Init_air_quality. The Init transmit is the live wiring/presence check:
    // a missing/unwired sensor NACKs -> non-OK -> we bail and the BLE relay continues.
    if ((e = sgp30_init(&s_sgp, bus, GAS_SCL_HZ)) != ESP_OK) {
        ha_mqtt_log("SGP30 NOT ready (init: %s) — check D4=SDA / D5=SCL / 3V3 / GND; BLE relay continues",
                    esp_err_to_name(e));
        return;
    }
    ha_mqtt_log("SGP30 up — eCO2/TVOC lane running (SDA=GPIO%d SCL=GPIO%d, 1Hz, ~15s warmup at 400/0)",
                GAS_SDA_GPIO, GAS_SCL_GPIO);
#else
    if ((e = sgp40_init(&s_sgp, bus, GAS_SCL_HZ)) != ESP_OK) {
        ha_mqtt_log("SGP40: add-device failed: %s", esp_err_to_name(e)); return;
    }
    // Self-test is also the live wiring check (fails if SDA/SCL/VCC aren't connected).
    if ((e = sgp40_self_test(&s_sgp)) != ESP_OK) {
        ha_mqtt_log("SGP40 NOT ready (self-test: %s) — check D4=SDA / D5=SCL / 3V3 / GND; BLE relay continues",
                    esp_err_to_name(e));
        return;
    }
    ha_mqtt_log("SGP40 self-test PASS — VOC lane up (SDA=GPIO%d SCL=GPIO%d, 1Hz, ~45s warmup)",
                GAS_SDA_GPIO, GAS_SCL_GPIO);
    GasIndexAlgorithm_init(&s_voc, GasIndexAlgorithm_ALGORITHM_TYPE_VOC);
#endif
    xTaskCreate(gas_task, "gas", 4096, NULL, 4, NULL);
}
