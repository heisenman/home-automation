// ha_partmig IDF executor — see include/ha_partmig.h for the step order and why it is that order.
#include "ha_partmig.h"

#include <string.h>

#include "esp_flash.h"
#include "esp_image_format.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ha_partmig";
static uint8_t s_cur[HA_PARTMIG_TABLE_MAX];   // static: 3 KB is too much for app_main's stack
static uint8_t s_chk[HA_PARTMIG_TABLE_MAX];

static esp_err_t erase(uint32_t off, uint32_t size) {
    if (!size) return ESP_OK;
    esp_err_t e = esp_flash_erase_region(NULL, off, size);
    ESP_LOGW(TAG, "erase 0x%06lx+0x%lx -> %s", (unsigned long)off, (unsigned long)size, esp_err_to_name(e));
    return e;
}

// erase the data partitions of one table that the migration must blank (nvs: foreign settings; otadata: so
// the bootloader falls through to ota_0 under the new table)
static void erase_data(const uint8_t *raw, size_t len) {
    ha_partmig_entry_t t[24];
    int n = ha_partmig_parse(raw, len, t, 24);
    for (int i = 0; i < n; i++)
        if (t[i].type == 0x01 && (t[i].subtype == 0x02 || t[i].subtype == 0x00)) erase(t[i].offset, t[i].size);
}

ha_partmig_verdict_t ha_partmig_run(const uint8_t *ours, size_t ours_len, void (*log)(const char *fmt, ...)) {
    char why[160];
    if (esp_flash_read(NULL, s_cur, HA_PARTMIG_TABLE_ADDR, sizeof s_cur) != ESP_OK) {
        ESP_LOGE(TAG, "cannot read the partition table — leaving it");
        return HA_PARTMIG_FOREIGN_UNSAFE;
    }
    const esp_partition_t *run = esp_ota_get_running_partition();
    esp_image_metadata_t md = {0};
    uint32_t image_len = 0;
    if (run && esp_image_get_metadata(&(esp_partition_pos_t){ .offset = run->address, .size = run->size }, &md) == ESP_OK)
        image_len = md.image_len;
    ha_partmig_verdict_t v = ha_partmig_plan(s_cur, sizeof s_cur, ours, ours_len, run ? run->address : 0,
                                             image_len, why, sizeof why);
    ESP_LOGW(TAG, "%s", why);
    if (log) log("partmig: %s", why);
    if (v != HA_PARTMIG_MIGRATE) return v;

    ESP_LOGW(TAG, "MIGRATING the partition table — do not remove power for the next second");
    erase_data(s_cur, sizeof s_cur);     // 1+2: Tasmota's nvs + otadata
    erase_data(ours, ours_len);          //      ours (the same ranges on a 4 MB Tasmota unit; harmless twice)
    for (int attempt = 1; attempt <= 3; attempt++) {   // 3: the table — the only unrecoverable window
        if (erase(HA_PARTMIG_TABLE_ADDR, 0x1000) != ESP_OK) continue;
        if (esp_flash_write(NULL, ours, HA_PARTMIG_TABLE_ADDR, ours_len) != ESP_OK) continue;
        if (esp_flash_read(NULL, s_chk, HA_PARTMIG_TABLE_ADDR, ours_len) == ESP_OK && memcmp(s_chk, ours, ours_len) == 0) {
            ESP_LOGW(TAG, "partition table rewritten + verified (attempt %d) — restarting into it", attempt);
            vTaskDelay(pdMS_TO_TICKS(100));
            esp_restart();   // 4
        }
        ESP_LOGE(TAG, "table verify failed (attempt %d)", attempt);
    }
    // Nothing more we can do safely: staying up keeps the image running (the table in RAM is still the old
    // one), and the next boot either finds a good table or needs the serial pads.
    ESP_LOGE(TAG, "PARTITION TABLE WRITE FAILED 3x — do not power-cycle; serial recovery likely needed");
    if (log) log("partmig: TABLE WRITE FAILED 3x — serial recovery likely needed");
    return HA_PARTMIG_FOREIGN_UNSAFE;
}
