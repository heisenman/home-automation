// Battery ADC discharge profiler — see bat_profile.h.
#include "bat_profile.h"
#include "bsp_display.h"

#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <sys/stat.h>
#include <unistd.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "driver/gpio.h"
#include "driver/sdmmc_host.h"
#include "sdmmc_cmd.h"
#include "esp_vfs_fat.h"
#include "sd_pwr_ctrl_by_on_chip_ldo.h"

static const char *TAG = "battprof";

#define SD_MOUNT   "/sdcard"
#define CSV_PATH   SD_MOUNT "/battprofile.csv"
#define SAMPLE_MS  15000       // 15 s cadence — ~240 rows/hr, hours of headroom on a 32 GB card
#define MQTT_EVERY 4           // mirror every 4th row (~1/min) to MQTT for live watch

// SDMMC drive pins (P4 Slot 0 uses IO-MUX; pins are fixed, we only trim drive strength).
#define SD_CLK    GPIO_NUM_43
#define SD_CMD    GPIO_NUM_44
#define SD_D0     GPIO_NUM_39
#define SD_D1     GPIO_NUM_40
#define SD_D2     GPIO_NUM_41
#define SD_D3     GPIO_NUM_42
#define SD_PWR_EN GPIO_NUM_46   // card VDD switch — MUST be high or the card never responds

static sdmmc_card_t *s_card;
static void (*s_publish)(const char *json);

// Mirror Seeed's bsp_sdcard_mount: Slot 0 (IO-MUX pins), on-chip LDO ch4 for card power,
// 4-bit bus, format-if-unmountable (so a blank recovery card just works).
static esp_err_t sd_mount(void)
{
    // Power the card's VDD (GPIO46). The LDO ch4 only feeds the SDMMC IO rail; without this
    // the card is dark and mount fails with "sdmmc_req: handle_idle_state_events". Power-cycle
    // it (Seeed's sequence) so a card that was left half-initialised comes up clean.
    gpio_config_t pc = { .pin_bit_mask = 1ULL << SD_PWR_EN, .mode = GPIO_MODE_OUTPUT };
    gpio_config(&pc);
    gpio_set_level(SD_PWR_EN, 0); vTaskDelay(pdMS_TO_TICKS(20));
    gpio_set_level(SD_PWR_EN, 1); vTaskDelay(pdMS_TO_TICKS(40));

    static sd_pwr_ctrl_handle_t pwr;
    if (!pwr) {
        sd_pwr_ctrl_ldo_config_t ldo = { .ldo_chan_id = 4 };
        esp_err_t lr = sd_pwr_ctrl_new_on_chip_ldo(&ldo, &pwr);
        if (lr != ESP_OK) ESP_LOGE(TAG, "sd ldo init: %s", esp_err_to_name(lr));
        vTaskDelay(pdMS_TO_TICKS(100));
    }

    sdmmc_host_t host = SDMMC_HOST_DEFAULT();
    host.slot = SDMMC_HOST_SLOT_0;
    host.max_freq_khz = SDMMC_FREQ_HIGHSPEED;
    host.pwr_ctrl_handle = pwr;

    sdmmc_slot_config_t slot = { .cd = SDMMC_SLOT_NO_CD, .wp = SDMMC_SLOT_NO_WP,
                                 .width = 4, .flags = 0 };
    esp_vfs_fat_sdmmc_mount_config_t mcfg = { .format_if_mount_failed = true,
                                              .max_files = 8, .allocation_unit_size = 64 * 1024 };

    esp_err_t r = esp_vfs_fat_sdmmc_mount(SD_MOUNT, &host, &slot, &mcfg, &s_card);
    if (r != ESP_OK) return r;

    // Reduce signal overshoot (Seeed BSP).
    gpio_set_drive_capability(SD_CLK, GPIO_DRIVE_CAP_1);
    gpio_set_drive_capability(SD_CMD, GPIO_DRIVE_CAP_1);
    gpio_set_drive_capability(SD_D0,  GPIO_DRIVE_CAP_1);
    gpio_set_drive_capability(SD_D1,  GPIO_DRIVE_CAP_1);
    gpio_set_drive_capability(SD_D2,  GPIO_DRIVE_CAP_1);
    gpio_set_drive_capability(SD_D3,  GPIO_DRIVE_CAP_1);
    return ESP_OK;
}

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
        bsp_batt_sample_t s = {0};
        if (bsp_battery_sample(&s) == ESP_OK) {
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
    esp_err_t r = sd_mount();
    if (r != ESP_OK) { ESP_LOGE(TAG, "SD mount failed: %s (profiler off)", esp_err_to_name(r)); return r; }
    if (s_card) {
        uint64_t mb = ((uint64_t)s_card->csd.capacity * s_card->csd.sector_size) >> 20;
        ESP_LOGW(TAG, "SD mounted at %s (%llu MB)", SD_MOUNT, (unsigned long long)mb);
    }
    if (xTaskCreate(profile_task, "battprof", 6144, NULL, 3, NULL) != pdPASS) {
        ESP_LOGE(TAG, "profile task create failed");
        return ESP_FAIL;
    }
    return ESP_OK;
}
