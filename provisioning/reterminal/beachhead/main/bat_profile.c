// Battery ADC discharge profiler — see bat_profile.h.
#include "bat_profile.h"
#include "ha_battery.h"

#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <sys/stat.h>
#include <unistd.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "ha_sdcard.h"

static const char *TAG = "battprof";

#define SD_MOUNT   "/sdcard"
#define CSV_PATH   SD_MOUNT "/battprofile.csv"
#define SAMPLE_MS  15000       // 15 s cadence — ~240 rows/hr, hours of headroom on a 32 GB card
#define MQTT_EVERY 4           // mirror every 4th row (~1/min) to MQTT for live watch

static void (*s_publish)(const char *json);

static void profile_task(void *pv)
{
    struct stat st;
    bool fresh = (stat(CSV_PATH, &st) != 0);
    FILE *f = fopen(CSV_PATH, "a");
    if (!f) { ESP_LOGE(TAG, "open %s failed: %s", CSV_PATH, strerror(errno)); vTaskDelete(NULL); return; }
    if (fresh)
        fprintf(f, "uptime_s,raw_ch2,cali_mv,batt_mv,batt_mv_smooth,usb_mv,vsys_pg,charging,soc,temp_dc,charge_en\n");
    fflush(f);
    ESP_LOGW(TAG, "profiling battery to %s every %ds", CSV_PATH, SAMPLE_MS / 1000);

    uint32_t n = 0;
    for (;;) {
        ha_batt_sample_t s = {0};
        if (ha_battery_sample(&s) == ESP_OK) {
            uint32_t up = (uint32_t)(esp_timer_get_time() / 1000000);
            int tdc = s.have_temp ? s.temp_dc : -9999;
            fprintf(f, "%lu,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d\n", (unsigned long)up,
                    s.raw_ch2, s.cali_mv, s.batt_mv, s.batt_mv_smoothed,
                    s.usb_mv, s.vsys_pg, s.charging, s.soc, tdc, s.charge_en);
            fflush(f);
            fsync(fileno(f));   // durable per row — a yanked card/power keeps the log so far

            if (s_publish && (n % MQTT_EVERY == 0)) {
                char j[256];
                snprintf(j, sizeof(j),
                    "{\"up\":%lu,\"raw\":%d,\"cali_mv\":%d,\"batt_mv\":%d,\"smooth_mv\":%d,"
                    "\"usb_mv\":%d,\"pg\":%d,\"chg\":%d,\"soc\":%d,\"temp_dc\":%d,\"charge_en\":%d}",
                    (unsigned long)up, s.raw_ch2, s.cali_mv, s.batt_mv, s.batt_mv_smoothed,
                    s.usb_mv, s.vsys_pg, s.charging, s.soc, tdc, s.charge_en);
                s_publish(j);
            }
            n++;
        } else {
            ESP_LOGW(TAG, "battery sample failed");
        }
        vTaskDelay(pdMS_TO_TICKS(SAMPLE_MS));
    }
}

esp_err_t bat_profile_start(void (*publish)(const char *json))
{
    s_publish = publish;
    esp_err_t r = ha_sdcard_mount(NULL);   // D1001 defaults (/sdcard, GPIO46, LDO ch4, slot 0)
    if (r != ESP_OK) { ESP_LOGE(TAG, "SD mount failed: %s (profiler off)", esp_err_to_name(r)); return r; }
    if (xTaskCreate(profile_task, "battprof", 6144, NULL, 3, NULL) != pdPASS) {
        ESP_LOGE(TAG, "profile task create failed");
        return ESP_FAIL;
    }
    return ESP_OK;
}
