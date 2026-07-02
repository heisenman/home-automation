#include "ha_gas.h"
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "driver/i2c_master.h"

#include "sgp40.h"                          // shared component: I2C driver
#include "sensirion_gas_index_algorithm.h" // shared component: VOC index algorithm
#include "ha_mqtt.h"

static const char *TAG = "ha_gas";

// Waveshare ESP32-S3-ETH I2C header pads: SDA = GPIO42, SCL = GPIO41 (Waveshare's I2C convention;
// clear of the W5500 SPI pins 9-14 and the WS2812 LED on 21). Grove SGP-40 carries its own 10k
// pull-ups; internal pull-ups enabled too as belt-and-braces. Note the ESP32-S3 has NO GPIO22/23,
// so the C6's pads don't exist here — these two #defines are the only board-specific change.
#define GAS_SDA_GPIO      42
#define GAS_SCL_GPIO      41
#define GAS_SCL_HZ        400000
#define GAS_I2C_PORT      I2C_NUM_0

#define GAS_SAMPLE_MS     1000     // Sensirion VOC algorithm REQUIRES a fixed 1 Hz cadence
#define GAS_PUBLISH_EVERY 10       // publish 1 in 10 samples -> a reading every ~10 s

// Registry key (payload "mac" — resolved by edge_mapper against instance/devices.yaml) and the topic
// segment. NOT a BLE MAC; the mapper does a plain dict lookup, so a readable key is fine.
#define GAS_TOPIC_KEY  "gas"
#define GAS_REG_KEY    "s3-crawlspace-gas"
#define GAS_DEV_TYPE   "sgp40_gas"

static sgp40_t s_sgp;
static GasIndexAlgorithmParams s_voc;

// One-shot I2C bus scan — the raw ACK proof, independent of the SGP-40 driver. Logs every 7-bit
// address that ACKs so a mis-solder (nothing / wrong pins) is instantly distinguishable from a
// driver-level fault. Expect exactly 0x59 (the SGP-40) on a good build.
static void gas_bus_scan(i2c_master_bus_handle_t bus) {
    char found[80]; int n = 0; found[0] = '\0';
    for (uint8_t addr = 0x08; addr <= 0x77; addr++) {
        if (i2c_master_probe(bus, addr, 50) == ESP_OK)
            n += snprintf(found + n, sizeof(found) - n, "%s0x%02X", n ? "," : "", addr);
        if (n >= (int)sizeof(found) - 6) break;
    }
    if (found[0]) ha_mqtt_log("SGP40 bus scan (SDA=GPIO%d SCL=GPIO%d): ACK %s",
                              GAS_SDA_GPIO, GAS_SCL_GPIO, found);
    else          ha_mqtt_log("SGP40 bus scan (SDA=GPIO%d SCL=GPIO%d): NO devices ACK — check wiring/power",
                              GAS_SDA_GPIO, GAS_SCL_GPIO);
}

static void gas_task(void *arg) {
    (void)arg;
    int since_pub = GAS_PUBLISH_EVERY;     // publish the first sample promptly
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(GAS_SAMPLE_MS));
        uint16_t sraw = 0;
        esp_err_t e = sgp40_measure_raw(&s_sgp, SGP40_DEFAULT_RH, SGP40_DEFAULT_T, &sraw);
        if (e != ESP_OK) { ESP_LOGW(TAG, "measure_raw: %s", esp_err_to_name(e)); continue; }

        int32_t voc = 0;                    // 0 during the first ~45 s blackout, then 1..500 (100 = baseline)
        GasIndexAlgorithm_process(&s_voc, (int32_t)sraw, &voc);

        if (++since_pub >= GAS_PUBLISH_EVERY) {
            since_pub = 0;
            char metrics[64];
            snprintf(metrics, sizeof(metrics),
                     "{\"voc_index\":%ld,\"voc_raw\":%u}", (long)voc, (unsigned)sraw);
            ha_mqtt_publish_node_sensor(GAS_TOPIC_KEY, GAS_REG_KEY, GAS_DEV_TYPE, metrics);
        }
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
    if (e != ESP_OK) { ha_mqtt_log("SGP40: I2C bus init failed: %s", esp_err_to_name(e)); return; }

    gas_bus_scan(bus);   // raw ACK proof before we trust the driver

    if ((e = sgp40_init(&s_sgp, bus, GAS_SCL_HZ)) != ESP_OK) {
        ha_mqtt_log("SGP40: add-device failed: %s", esp_err_to_name(e)); return;
    }

    // Self-test is also the live wiring check (fails if SDA/SCL/VCC aren't connected).
    if ((e = sgp40_self_test(&s_sgp)) != ESP_OK) {
        ha_mqtt_log("SGP40 NOT ready (self-test: %s) — check SDA=GPIO%d / SCL=GPIO%d / 3V3 / GND; BLE relay continues",
                    esp_err_to_name(e), GAS_SDA_GPIO, GAS_SCL_GPIO);
        return;
    }
    ha_mqtt_log("SGP40 self-test PASS — VOC lane up (SDA=GPIO%d SCL=GPIO%d, 1Hz, ~45s warmup)",
                GAS_SDA_GPIO, GAS_SCL_GPIO);

    GasIndexAlgorithm_init(&s_voc, GasIndexAlgorithm_ALGORITHM_TYPE_VOC);
    xTaskCreate(gas_task, "gas", 4096, NULL, 4, NULL);
}
