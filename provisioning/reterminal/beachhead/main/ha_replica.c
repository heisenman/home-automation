// Panel data replica — ADR-0022 Phase 1a seed pull (see ha_replica.h).
#include "ha_replica.h"
#include <string.h>
#include <stdio.h>
#include <sys/stat.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_http_client.h"
#include "cJSON.h"
#include "ha_sdcard.h"

static const char *TAG = "ha.replica";

// Full.db is ~38 MB (all rungs) and its sha256 changes every rollup (~5 min, the 1min rung always
// grows), so ANY poll finds a change and re-pulls the WHOLE file. Phase 1a has only full re-pull
// (the `since` delta is Phase 2), so we refresh SLOWLY: a wall panel's 72h history tolerates ~1h
// staleness, and this keeps the 38 MB pull to ~once/hour instead of every 5 min. Phase 2 makes
// frequent freshness cheap.
#define POLL_MS   (60 * 60 * 1000)  // 1h — cold-start pulls immediately; then hourly refresh
#define BOOT_MS   8000              // let WiFi + SD settle before the first pull
#define DB_NAME   "rungs.db"
#define SHA_NAME  "rungs.db.sha"    // sidecar: sha256 of the last-synced full.db (the HWM)

static char s_base[160];

// Small GET into a caller buffer (manifest.json). Returns body length, or -1 on any failure.
static int http_get(const char *url, char *out, int cap)
{
    esp_http_client_config_t cfg = { .url = url, .timeout_ms = 15000 };
    esp_http_client_handle_t cl = esp_http_client_init(&cfg);
    if (!cl) return -1;
    int len = -1;
    if (esp_http_client_open(cl, 0) == ESP_OK) {
        esp_http_client_fetch_headers(cl);
        if (esp_http_client_get_status_code(cl) == 200) {
            int n = esp_http_client_read_response(cl, out, cap - 1);
            if (n >= 0) { out[n] = 0; len = n; }
        }
    }
    esp_http_client_close(cl);
    esp_http_client_cleanup(cl);
    return len;
}

// Stream a GET body straight to `path` (full.db is MB-scale — never buffer it in RAM). True on success.
static bool http_get_to_file(const char *url, const char *path)
{
    esp_http_client_config_t cfg = { .url = url, .timeout_ms = 30000 };
    esp_http_client_handle_t cl = esp_http_client_init(&cfg);
    if (!cl) return false;
    bool ok = false;
    if (esp_http_client_open(cl, 0) == ESP_OK) {
        esp_http_client_fetch_headers(cl);
        int status = esp_http_client_get_status_code(cl);
        if (status == 200) {
            FILE *f = fopen(path, "wb");
            if (f) {
                static char buf[2048];   // task-private (single sync task) — keeps the stack small
                int r; size_t total = 0; ok = true;
                while ((r = esp_http_client_read(cl, buf, sizeof buf)) > 0) {
                    if (fwrite(buf, 1, r, f) != (size_t)r) { ok = false; break; }
                    total += r;
                }
                if (r < 0) ok = false;   // transport error mid-stream
                fclose(f);
                if (ok) ESP_LOGI(TAG, "pulled %u bytes", (unsigned)total);
            } else ESP_LOGW(TAG, "cannot open %s for write", path);
        } else ESP_LOGW(TAG, "GET full.db -> HTTP %d", status);
    }
    esp_http_client_close(cl);
    esp_http_client_cleanup(cl);
    return ok;
}

static void sync_once(void)
{
    if (!ha_sdcard_mounted()) { ESP_LOGW(TAG, "SD not mounted — skip this cycle"); return; }
    const char *mp = ha_sdcard_mount_point();
    char db[128], sha[128], tmp[136], url[224];
    static char man[2048];
    snprintf(db,  sizeof db,  "%s/%s", mp, DB_NAME);
    snprintf(sha, sizeof sha, "%s/%s", mp, SHA_NAME);
    snprintf(tmp, sizeof tmp, "%s/%s.tmp", mp, DB_NAME);

    // 1. manifest — cheap; tells us the server DB's sha256 without pulling it
    snprintf(url, sizeof url, "%s/api/v1/rung/manifest.json", s_base);
    if (http_get(url, man, sizeof man) <= 0) {
        ESP_LOGW(TAG, "manifest fetch failed (server rungs.db not built yet?)");
        return;
    }
    cJSON *j = cJSON_Parse(man);
    cJSON *jsha = j ? cJSON_GetObjectItem(j, "sha256") : NULL;
    char want[72] = "";
    if (cJSON_IsString(jsha)) snprintf(want, sizeof want, "%s", jsha->valuestring);
    if (j) cJSON_Delete(j);
    if (!want[0]) { ESP_LOGW(TAG, "manifest missing sha256"); return; }

    // 2. compare against the local sidecar + DB presence
    char have[72] = "";
    FILE *sf = fopen(sha, "r");
    if (sf) { if (fgets(have, sizeof have, sf)) have[strcspn(have, "\r\n")] = 0; fclose(sf); }
    struct stat st;
    bool db_present = (stat(db, &st) == 0 && st.st_size > 0);
    if (db_present && strcmp(have, want) == 0) {
        ESP_LOGI(TAG, "replica up to date (sha %.12s…)", want);
        return;
    }

    // 3. pull full.db via a temp file, then atomically swap in (a partial download never corrupts rungs.db)
    ESP_LOGI(TAG, "replica behind — pulling full.db (%s)", db_present ? "changed" : "cold start");
    snprintf(url, sizeof url, "%s/api/v1/rung/full.db", s_base);
    if (!http_get_to_file(url, tmp)) { remove(tmp); return; }
    remove(db);
    if (rename(tmp, db) != 0) { ESP_LOGW(TAG, "rename %s -> %s failed", tmp, db); remove(tmp); return; }
    sf = fopen(sha, "w");
    if (sf) { fprintf(sf, "%s\n", want); fclose(sf); }
    ESP_LOGI(TAG, "replica updated -> %s (sha %.12s…)", db, want);
}

static void replica_task(void *pv)
{
    vTaskDelay(pdMS_TO_TICKS(BOOT_MS));
    for (;;) {
        sync_once();
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    }
}

void ha_replica_start(const char *base)
{
    snprintf(s_base, sizeof s_base, "%s", base);
    xTaskCreate(replica_task, "ha_replica", 8192, NULL, 3, NULL);
    ESP_LOGI(TAG, "ha_replica start -> %s", s_base);
}
