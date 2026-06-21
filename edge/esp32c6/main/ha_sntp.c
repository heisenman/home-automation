#include "ha_sntp.h"
#include <time.h>
#include <string.h>
#include "esp_netif_sntp.h"
#include "esp_log.h"

static const char *TAG = "ha_sntp";

bool ha_sntp_sync(const char *server, int timeout_ms) {
    esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG(server);
    cfg.start = true;
    cfg.sync_cb = NULL;
    esp_netif_sntp_init(&cfg);
    if (esp_netif_sntp_sync_wait(pdMS_TO_TICKS(timeout_ms)) != ESP_OK) {
        ESP_LOGW(TAG, "SNTP sync timeout against %s", server);
        return false;
    }
    time_t now = 0; time(&now);
    ESP_LOGI(TAG, "time set: %lld", (long long)now);
    return true;
}

bool ha_sntp_iso_utc(char *buf, int buf_len) {
    time_t now = 0; time(&now);
    if (now < 1700000000) return false;   // clock not set yet
    struct tm tm_utc;
    gmtime_r(&now, &tm_utc);
    strftime(buf, buf_len, "%Y-%m-%dT%H:%M:%SZ", &tm_utc);
    return true;
}
