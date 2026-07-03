// SD card mount — see ha_sdcard.h.
#include "ha_sdcard.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "driver/gpio.h"
#include "driver/sdmmc_host.h"
#include "sdmmc_cmd.h"
#include "esp_vfs_fat.h"
#include "sd_pwr_ctrl_by_on_chip_ldo.h"

static const char *TAG = "ha_sdcard";

// P4 Slot 0 uses IO-MUX (fixed pins); we only trim drive strength to reduce overshoot.
#define SD_CLK  GPIO_NUM_43
#define SD_CMD  GPIO_NUM_44
#define SD_D0   GPIO_NUM_39
#define SD_D1   GPIO_NUM_40
#define SD_D2   GPIO_NUM_41
#define SD_D3   GPIO_NUM_42

static const ha_sdcard_cfg_t DEFAULTS = {
    .mount_point = "/sdcard",
    .pwr_en_gpio = 46,
    .ldo_chan_id = 4,
    .slot = SDMMC_HOST_SLOT_0,
    .width = 4,
    .max_freq_khz = SDMMC_FREQ_HIGHSPEED,
    .format_if_mount_failed = true,
};

static sdmmc_card_t *s_card;
static const char *s_mount;
static bool s_mounted;

// The P4 has ONE SDMMC host, shared: the C6 WiFi SDIO link owns slot 1 (esp-hosted), this card
// mounts slot 0. On a mount failure (no/dead card), esp_vfs_fat_sdmmc_mount() runs its cleanup
// and calls host.deinit_p(slot) == sdmmc_host_deinit_slot(0), which frees the host-GLOBAL
// transaction semaphore — so the next WiFi SDIO transaction on slot 1 hits xQueueSemaphoreTake
// (NULL) and panics (bootloops when no SD card is inserted). We do NOT own the host lifecycle
// (esp-hosted does), so neuter both deinit hooks: a card miss then leaves the shared host — and
// slot 1 — untouched. Nothing unmounts the card at runtime, so losing deinit costs us nothing.
static esp_err_t sd_host_deinit_noop(void)        { return ESP_OK; }
static esp_err_t sd_host_deinit_slot_noop(int slot){ (void)slot; return ESP_OK; }

esp_err_t ha_sdcard_mount(const ha_sdcard_cfg_t *cfg)
{
    if (s_mounted) return ESP_OK;
    const ha_sdcard_cfg_t c = cfg ? *cfg : DEFAULTS;
    const char *mp = c.mount_point ? c.mount_point : "/sdcard";

    // Power the card's VDD. The LDO only feeds the SDMMC IO rail; without this the card is
    // dark and mount fails. Power-cycle it so a half-initialised card comes up clean.
    if (c.pwr_en_gpio >= 0) {
        gpio_config_t pc = { .pin_bit_mask = 1ULL << c.pwr_en_gpio, .mode = GPIO_MODE_OUTPUT };
        gpio_config(&pc);
        gpio_set_level(c.pwr_en_gpio, 0); vTaskDelay(pdMS_TO_TICKS(20));
        gpio_set_level(c.pwr_en_gpio, 1); vTaskDelay(pdMS_TO_TICKS(40));
    }

    static sd_pwr_ctrl_handle_t pwr;
    if (!pwr && c.ldo_chan_id >= 0) {
        sd_pwr_ctrl_ldo_config_t ldo = { .ldo_chan_id = c.ldo_chan_id };
        esp_err_t lr = sd_pwr_ctrl_new_on_chip_ldo(&ldo, &pwr);
        if (lr != ESP_OK) ESP_LOGE(TAG, "sd ldo init: %s", esp_err_to_name(lr));
        vTaskDelay(pdMS_TO_TICKS(100));
    }

    sdmmc_host_t host = SDMMC_HOST_DEFAULT();
    host.slot = c.slot;
    host.max_freq_khz = c.max_freq_khz;
    host.pwr_ctrl_handle = pwr;
    host.deinit   = sd_host_deinit_noop;        // never tear down the shared host on a card miss
    host.deinit_p = sd_host_deinit_slot_noop;   // (SDMMC_HOST_FLAG_DEINIT_ARG picks this one)

    sdmmc_slot_config_t slot = { .cd = SDMMC_SLOT_NO_CD, .wp = SDMMC_SLOT_NO_WP,
                                 .width = (uint8_t)c.width, .flags = 0 };
    esp_vfs_fat_sdmmc_mount_config_t mcfg = { .format_if_mount_failed = c.format_if_mount_failed,
                                              .max_files = 8, .allocation_unit_size = 64 * 1024 };

    esp_err_t r = esp_vfs_fat_sdmmc_mount(mp, &host, &slot, &mcfg, &s_card);
    if (r != ESP_OK) { ESP_LOGE(TAG, "mount %s failed: %s", mp, esp_err_to_name(r)); return r; }

    gpio_set_drive_capability(SD_CLK, GPIO_DRIVE_CAP_1);
    gpio_set_drive_capability(SD_CMD, GPIO_DRIVE_CAP_1);
    gpio_set_drive_capability(SD_D0,  GPIO_DRIVE_CAP_1);
    gpio_set_drive_capability(SD_D1,  GPIO_DRIVE_CAP_1);
    gpio_set_drive_capability(SD_D2,  GPIO_DRIVE_CAP_1);
    gpio_set_drive_capability(SD_D3,  GPIO_DRIVE_CAP_1);

    s_mount = mp;
    s_mounted = true;
    ESP_LOGW(TAG, "mounted at %s (%llu MB)", mp, ha_sdcard_size_mb());
    return ESP_OK;
}

bool ha_sdcard_mounted(void) { return s_mounted; }
const char *ha_sdcard_mount_point(void) { return s_mounted ? s_mount : NULL; }

uint64_t ha_sdcard_size_mb(void)
{
    if (!s_mounted || !s_card) return 0;
    return ((uint64_t)s_card->csd.capacity * s_card->csd.sector_size) >> 20;
}

// ─────────────────────────────── hot-plug watcher ───────────────────────────────
#include "freertos/task.h"

static ha_sdcard_cfg_t s_watch_cfg;
static bool s_watch_cfg_set;
static int  s_detect_gpio = -1;
static bool s_detect_active_low = true;
static void (*s_on_change)(bool present);
static TaskHandle_t s_watch_task;

bool ha_sdcard_present(void) { return s_mounted; }

void ha_sdcard_unmount(void)
{
    if (!s_mounted) return;
    // card->host carries our no-op deinit (set at mount), so this frees the FAT/slot but leaves
    // the SDMMC host shared with the C6 WiFi SDIO alone.
    esp_vfs_fat_sdcard_unmount(s_mount ? s_mount : "/sdcard", s_card);
    s_card = NULL;
    s_mounted = false;
    ESP_LOGW(TAG, "unmounted");
}

static bool detect_present(void)
{
    if (s_detect_gpio < 0) return s_mounted;
    int lvl = gpio_get_level(s_detect_gpio);
    return s_detect_active_low ? (lvl == 0) : (lvl != 0);
}

// GPIO ISR runs from flash (isr service installed with flag 0) — no IRAM_ATTR needed. Just nudge
// the watcher task; all mount/unmount work happens there.
static void detect_isr(void *arg)
{
    (void)arg;
    BaseType_t hp = pdFALSE;
    if (s_watch_task) vTaskNotifyGiveFromISR(s_watch_task, &hp);
    portYIELD_FROM_ISR(hp);
}

static void watch_task(void *pv)
{
    (void)pv;
    bool announced = false, last = false;
    for (;;) {
        bool present = detect_present();
        if (present && !s_mounted) {
            ESP_LOGW(TAG, "card detected -> mounting");
            if (ha_sdcard_mount(s_watch_cfg_set ? &s_watch_cfg : NULL) != ESP_OK)
                present = false;                    // mount failed: not usable
        } else if (!present && s_mounted) {
            ESP_LOGW(TAG, "card removed -> unmounting");
            ha_sdcard_unmount();
        }
        bool usable = s_mounted;                     // the presence global consumers gate on
        if (!announced || usable != last) {
            if (s_on_change) s_on_change(usable);
            last = usable; announced = true;
        }
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(3000)); // edge, or self-heal poll every 3 s
        vTaskDelay(pdMS_TO_TICKS(150));                // debounce settle before re-reading
    }
}

void ha_sdcard_watch(const ha_sdcard_cfg_t *cfg, int detect_gpio, bool active_low,
                     void (*on_change)(bool present))
{
    if (s_watch_task) return;                         // once per boot
    if (cfg) { s_watch_cfg = *cfg; s_watch_cfg_set = true; }
    s_detect_gpio = detect_gpio;
    s_detect_active_low = active_low;
    s_on_change = on_change;

    xTaskCreate(watch_task, "sdwatch", 6144, NULL, 4, &s_watch_task);

    if (detect_gpio >= 0) {
        gpio_config_t g = { .pin_bit_mask = 1ULL << detect_gpio, .mode = GPIO_MODE_INPUT,
                            .pull_up_en = GPIO_PULLUP_ENABLE, .intr_type = GPIO_INTR_ANYEDGE };
        gpio_config(&g);
        esp_err_t ir = gpio_install_isr_service(0);
        if (ir != ESP_OK && ir != ESP_ERR_INVALID_STATE)
            ESP_LOGW(TAG, "sd-detect isr service: %s", esp_err_to_name(ir));
        gpio_isr_handler_add(detect_gpio, detect_isr, NULL);
        ESP_LOGW(TAG, "SD-detect watch on GPIO%d (boot level=%d, active-%s)",
                 detect_gpio, gpio_get_level(detect_gpio), active_low ? "low" : "high");
    }
}
