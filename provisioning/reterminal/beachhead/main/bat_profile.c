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
#define SAMPLE_MS_DEFAULT 15000   // 15 s cadence — ~240 rows/hr, hours of headroom on a 32 GB card
#define CSV_HEADER "uptime_s,raw_ch2,cali_mv,batt_mv,batt_mv_smooth,usb_mv,vsys_pg,charging,soc,temp_dc,charge_en\n"

static void (*s_publish)(const char *json);
static volatile int s_sample_ms = SAMPLE_MS_DEFAULT;   // runtime cadence (cmd/battrate); finer over transitions

int bat_profile_set_rate(int ms)
{
    if (ms < 200)    ms = 200;      // 5 Hz ceiling — protects the bus + SD from a runaway rate
    if (ms > 300000) ms = 300000;   // 5 min floor
    s_sample_ms = ms;
    return ms;
}

// Append one row to the SD CSV — open/append/close PER ROW, presence-gated. No persistent handle,
// so a hot-unplug can never leave a dead FILE* or spam write timeouts; the row is just skipped.
static void csv_append(uint32_t up, const ha_batt_sample_t *s, int tdc)
{
    if (!ha_sdcard_present()) return;
    struct stat st;
    bool fresh = (stat(CSV_PATH, &st) != 0);
    FILE *f = fopen(CSV_PATH, "a");
    if (!f) return;                 // card yanked between the check and open → skip this row
    if (fresh) fputs(CSV_HEADER, f);
    fprintf(f, "%lu,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d\n", (unsigned long)up,
            s->raw_ch2, s->cali_mv, s->batt_mv, s->batt_mv_smoothed,
            s->usb_mv, s->vsys_pg, s->charging, s->soc, tdc, s->charge_en);
    fclose(f);
}

static void profile_task(void *pv)
{
    ESP_LOGW(TAG, "battery profiler: MQTT %s every %dms + CSV to %s when SD present",
             "d1001-beachhead/battprofile", s_sample_ms, CSV_PATH);
    for (;;) {
        ha_batt_sample_t s = {0};
        if (ha_battery_sample(&s) == ESP_OK) {
            uint32_t up = (uint32_t)(esp_timer_get_time() / 1000000);
            int tdc = s.have_temp ? s.temp_dc : -9999;

            // MQTT EVERY cycle — the reliable, SD-independent raw stream to log during a
            // charge/discharge run (raw ADC + cali_mv + batt_mv, not just the LUT'd soc).
            if (s_publish) {
                char j[256];
                snprintf(j, sizeof(j),
                    "{\"up\":%lu,\"raw\":%d,\"cali_mv\":%d,\"batt_mv\":%d,\"smooth_mv\":%d,"
                    "\"usb_mv\":%d,\"pg\":%d,\"chg\":%d,\"soc\":%d,\"temp_dc\":%d,\"charge_en\":%d}",
                    (unsigned long)up, s.raw_ch2, s.cali_mv, s.batt_mv, s.batt_mv_smoothed,
                    s.usb_mv, s.vsys_pg, s.charging, s.soc, tdc, s.charge_en);
                s_publish(j);
            }
            csv_append(up, &s, tdc);
        } else {
            ESP_LOGW(TAG, "battery sample failed");
        }
        vTaskDelay(pdMS_TO_TICKS(s_sample_ms));
    }
}

esp_err_t bat_profile_start(void (*publish)(const char *json))
{
    s_publish = publish;
    // Do NOT hard-require SD: the MQTT raw stream must run regardless (it's the reliable channel).
    // The SD CSV is a presence-gated bonus; the hot-plug watcher owns mounting.
    if (xTaskCreate(profile_task, "battprof", 6144, NULL, 3, NULL) != pdPASS) {
        ESP_LOGE(TAG, "profile task create failed");
        return ESP_FAIL;
    }
    return ESP_OK;
}
