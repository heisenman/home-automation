// Panel data replica — ADR-0022 Phase 1a seed pull (see ha_replica.h).
#include "ha_replica.h"
#include <string.h>
#include <stdio.h>
#include <sys/stat.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_http_client.h"
#include "cJSON.h"
#include "ha_sdcard.h"
#include "sqlite3.h"

static const char *TAG = "ha.replica";

// Serializes the two ways rungs.db is touched: this module's whole-file swap (writer, ha_replica
// task) and Phase-1b chart queries (readers, chart_worker task). Required because FATFS rename over
// an open file is undefined and the vendored sqlite VFS uses no-op locking (single-access contract).
static SemaphoreHandle_t s_db_mutex;
static volatile bool s_db_ready;     // a valid rungs.db is present on SD (latched once true)
static char s_db_path[128];          // "/sdcard/rungs.db" (set once the SD mount is known)

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
    snprintf(s_db_path, sizeof s_db_path, "%s", db);   // publish the path for rung queries

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
        s_db_ready = true;   // an existing, current DB is usable by chart queries
        ESP_LOGI(TAG, "replica up to date (sha %.12s…)", want);
        return;
    }

    // 3. pull full.db via a temp file, then atomically swap in (a partial download never corrupts rungs.db)
    ESP_LOGI(TAG, "replica behind — pulling full.db (%s)", db_present ? "changed" : "cold start");
    snprintf(url, sizeof url, "%s/api/v1/rung/full.db", s_base);
    if (!http_get_to_file(url, tmp)) { remove(tmp); return; }
    // swap in under the DB mutex so a concurrent chart query never reads a half-swapped file
    if (s_db_mutex) xSemaphoreTake(s_db_mutex, portMAX_DELAY);
    remove(db);
    int rr = rename(tmp, db);
    if (rr == 0) s_db_ready = true;
    if (s_db_mutex) xSemaphoreGive(s_db_mutex);
    if (rr != 0) { ESP_LOGW(TAG, "rename %s -> %s failed", tmp, db); remove(tmp); return; }
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
    if (!s_db_mutex) s_db_mutex = xSemaphoreCreateMutex();
    xTaskCreate(replica_task, "ha_replica", 8192, NULL, 3, NULL);
    ESP_LOGI(TAG, "ha_replica start -> %s", s_base);
}

// span (hours) → rung name + row limit, mirroring server rollup.select_resolution. The panel has no
// raw archive, so the ≤2h "raw" bracket collapses into 1min (its finest rung).
static const char *pick_res(int hours, int *limit)
{
    long span = (long)hours * 3600;
    const char *res; int secs;
    if      (span <= 2 * 86400)  { res = "1min";  secs = 60;    }
    else if (span <= 60 * 86400) { res = "1hour"; secs = 3600;  }
    else                         { res = "1day";  secs = 86400; }
    *limit = (int)(span / secs);
    return res;
}

int ha_replica_rung_query(const char *device_id, const char *metric, int hours, double *out, int cap)
{
    if (!s_db_ready || !s_db_mutex || !s_db_path[0]) return -1;
    int limit; const char *res = pick_res(hours, &limit);
    if (limit > cap) limit = cap;
    if (xSemaphoreTake(s_db_mutex, pdMS_TO_TICKS(300)) != pdTRUE) return -1;   // busy → let caller fall back

    int n = -1;
    sqlite3 *db = NULL;
    if (sqlite3_open_v2(s_db_path, &db, SQLITE_OPEN_READONLY, NULL) == SQLITE_OK) {
        sqlite3_stmt *st = NULL;
        // PK is (res,device_id,metric,bucket_start) WITHOUT ROWID → this is a covering scan of the
        // btree tail (fast: 9.6 ms warm on this P4, sqlite3 README). DESC+LIMIT = the most recent N.
        static const char *q =
            "SELECT vmean FROM rung WHERE res=?1 AND device_id=?2 AND metric=?3 AND vmean IS NOT NULL "
            "ORDER BY bucket_start DESC LIMIT ?4";
        if (sqlite3_prepare_v2(db, q, -1, &st, NULL) == SQLITE_OK) {
            sqlite3_bind_text(st, 1, res, -1, SQLITE_STATIC);
            sqlite3_bind_text(st, 2, device_id, -1, SQLITE_STATIC);
            sqlite3_bind_text(st, 3, metric, -1, SQLITE_STATIC);
            sqlite3_bind_int(st, 4, limit);
            int m = 0;
            while (m < cap && sqlite3_step(st) == SQLITE_ROW) out[m++] = sqlite3_column_double(st, 0);
            for (int i = 0; i < m / 2; i++) {   // rows arrived newest→oldest; reverse to oldest→newest
                double t = out[i]; out[i] = out[m - 1 - i]; out[m - 1 - i] = t;
            }
            n = m;
            sqlite3_finalize(st);
        }
        sqlite3_close(db);
    }
    xSemaphoreGive(s_db_mutex);
    return n;
}
