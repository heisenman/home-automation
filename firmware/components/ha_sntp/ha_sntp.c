#include "ha_sntp.h"
#include <time.h>
#include <string.h>
#include <stdio.h>
#include "esp_netif_sntp.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ha_sntp";
static bool s_inited = false;
static char s_server[64];

static char s_fallback[64];          // the MQTT broker's host — reachable by construction (2026-10-12, below)

void ha_sntp_set_fallback_from_uri(const char *uri) {
    // "mqtt://host:port" → "host". Why: a generic image flashed for the air-gap carried NVS ntp_server =
    // pool.ntp.org (the flasher's one global default), which that network cannot reach. Its clock never set,
    // so EVERY signed command — OTA included — failed the freshness check: sgp41_stdby2 + sgp41_mech sat
    // clock-locked for days with data still flowing. Every node can reach its broker, and the dictator that
    // runs the broker runs NTP (chrony), so the broker host is the one time source that can't be wrong.
    s_fallback[0] = '\0';
    if (!uri) return;
    const char *h = strstr(uri, "://");
    h = h ? h + 3 : uri;
    size_t n = strcspn(h, ":/");
    if (n == 0 || n >= sizeof(s_fallback)) return;
    memcpy(s_fallback, h, n);
    s_fallback[n] = '\0';
}

static bool sntp_try(const char *server, int timeout_ms) {
    if (s_inited) esp_netif_sntp_deinit();          // re-init for a fresh sync (init can't run twice)
    esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG(server);
    cfg.start = true;
    cfg.sync_cb = NULL;
    esp_netif_sntp_init(&cfg);
    s_inited = true;
    if (esp_netif_sntp_sync_wait(pdMS_TO_TICKS(timeout_ms)) != ESP_OK) {
        ESP_LOGW(TAG, "SNTP sync timeout against %s", server);
        return false;
    }
    time_t now = 0; time(&now);
    ESP_LOGI(TAG, "time set from %s: %lld", server, (long long)now);
    return true;
}

bool ha_sntp_sync(const char *server, int timeout_ms) {
    snprintf(s_server, sizeof(s_server), "%s", server);
    if (sntp_try(server, timeout_ms)) return true;
    if (s_fallback[0] && strcmp(s_fallback, server) != 0) {
        ESP_LOGW(TAG, "falling back to the broker host %s for time", s_fallback);
        return sntp_try(s_fallback, timeout_ms);
    }
    return false;
}

// Periodic re-sync. The C6's RTC (internal RC oscillator — no external 32 kHz crystal) drifts fast, so a
// boot-only sync lets the clock wander far enough to fail the command freshness check — a clock-lockout
// that even blocks the OTA that would fix it. Re-syncing on an interval keeps the clock bounded.
static int s_interval_ms;
static void sntp_periodic_task(void *arg) {
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(s_interval_ms));
        if (ha_sntp_sync(s_server, 15000)) ESP_LOGI(TAG, "periodic SNTP re-sync ok");
        else ESP_LOGW(TAG, "periodic SNTP re-sync failed (will retry next interval)");
    }
}

void ha_sntp_start_periodic(int interval_ms) {
    if (interval_ms < 60000) interval_ms = 60000;
    s_interval_ms = interval_ms;
    xTaskCreate(sntp_periodic_task, "sntp_resync", 4096, NULL, 3, NULL);
}

bool ha_sntp_iso_utc(char *buf, int buf_len) {
    time_t now = 0; time(&now);
    if (now < 1700000000) return false;   // clock not set yet
    struct tm tm_utc;
    gmtime_r(&now, &tm_utc);
    strftime(buf, buf_len, "%Y-%m-%dT%H:%M:%SZ", &tm_utc);
    return true;
}
