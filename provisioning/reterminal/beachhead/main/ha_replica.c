// Panel data replica — ADR-0022 Phase 2 (incremental `since` sync). See ha_replica.h.
#include "ha_replica.h"
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include "esp_vfs_fat.h"     // esp_vfs_fat_info() — FATFS free space (ESP-IDF has no statvfs)
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_http_client.h"
#include "cJSON.h"
#include "ha_sdcard.h"
#include "sqlite3.h"

static const char *TAG = "ha.replica";

// Serializes the two ways rungs.db is touched: this module's writer (seed swap + incremental upsert,
// on the ha_replica task) and Phase-1b chart queries (readers, chart_worker task). Required because
// the vendored sqlite VFS uses no-op locking (single-access contract) — only ONE connection may be
// open at a time — and a FATFS rename over an open file is undefined.
static SemaphoreHandle_t s_db_mutex;
static volatile bool s_db_ready;     // a valid rungs.db is present on SD (latched once true)
static char s_db_path[128];          // "/sdcard/rungs.db" (set once the SD mount is known)

// Phase 2: after a one-time full.db seed, each cycle pulls only the rows past the local per-rung
// high-water-mark via /rung/since (NDJSON) and upserts them — the 38 MB full re-pull is gone (that
// was Phase 1a), so freshening is cheap and can run often instead of hourly.
#define POLL_MS   (10 * 60 * 1000)  // 10 min — cold-start seeds immediately; then incremental deltas
#define BOOT_MS   8000              // let WiFi + SD settle before the first pull
#define DB_NAME   "rungs.db"

// The rung ladder, finest→coarsest. `secs` = bucket width (for span→rung selection + row limits).
// Names + thresholds mirror server rollup.select_resolution (rollup.py:48). The panel has no raw
// archive, so the server's ≤2h "raw" bracket collapses into 1min (its finest rung).
static const struct { const char *name; long secs; } RUNGS[] = {
    { "1min",  60     },
    { "1hour", 3600   },
    { "1day",  86400  },
    { "1week", 604800 },
};
#define NRUNGS ((int)(sizeof(RUNGS) / sizeof(RUNGS[0])))

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

// Stream a GET body straight to `path` (full.db is MB-scale; a since-delta is small but still
// streamed so RAM never bounds it). True on success. Called only on the ha_replica task.
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
        } else ESP_LOGW(TAG, "GET -> HTTP %d", status);
    }
    esp_http_client_close(cl);
    esp_http_client_cleanup(cl);
    return ok;
}

// span (seconds) → starting rung index, mirroring server select_resolution (panel: raw→1min).
static int start_rung(long span_s)
{
    if (span_s <= 2L * 86400)       return 0;  // ≤2d  → 1min
    if (span_s <= 60L * 86400)      return 1;  // ≤2mo → 1hour
    if (span_s <= 4L * 365 * 86400) return 2;  // ≤4y  → 1day
    return 3;                                   //      → 1week
}

// Local per-rung high-water-mark: MAX(bucket_start) for `res`, or -1 if the rung is empty / unreadable.
// Caller holds s_db_mutex (this opens a short-lived RO connection — single-access sqlite contract).
static long local_hwm(const char *res)
{
    long hwm = -1;
    sqlite3 *db = NULL;
    if (sqlite3_open_v2(s_db_path, &db, SQLITE_OPEN_READONLY, NULL) == SQLITE_OK) {
        sqlite3_stmt *st = NULL;
        if (sqlite3_prepare_v2(db, "SELECT MAX(bucket_start) FROM rung WHERE res=?1", -1, &st, NULL) == SQLITE_OK) {
            sqlite3_bind_text(st, 1, res, -1, SQLITE_STATIC);
            if (sqlite3_step(st) == SQLITE_ROW && sqlite3_column_type(st, 0) != SQLITE_NULL)
                hwm = (long)sqlite3_column_int64(st, 0);
            sqlite3_finalize(st);
        }
        sqlite3_close(db);
    }
    return hwm;
}

// Pull `res` rows with bucket_start >= `after` (INCLUSIVE — the boundary bucket keeps updating until
// it closes, so it is always re-sent and re-upserted) and upsert them into the local rung table.
// Network stream → temp NDJSON file (no lock), then a single locked upsert transaction. Returns rows
// applied, or -1 on failure. ha_replica task only.
static int sync_rung(const char *res, long after, const char *tmp_ndjson)
{
    char url[240];
    snprintf(url, sizeof url, "%s/api/v1/rung/since?res=%s&after=%ld", s_base, res, after < 0 ? 0 : after);
    if (!http_get_to_file(url, tmp_ndjson)) { remove(tmp_ndjson); return -1; }

    FILE *f = fopen(tmp_ndjson, "r");
    if (!f) { remove(tmp_ndjson); return -1; }

    int applied = 0, rc = SQLITE_OK;
    if (s_db_mutex) xSemaphoreTake(s_db_mutex, portMAX_DELAY);
    sqlite3 *db = NULL;
    if (sqlite3_open_v2(s_db_path, &db, SQLITE_OPEN_READWRITE, NULL) == SQLITE_OK) {
        sqlite3_exec(db, "BEGIN", NULL, NULL, NULL);
        static const char *ins =
            "INSERT OR REPLACE INTO rung (res,device_id,metric,bucket_start,vmin,vmax,vmean,vcount,vlast) "
            "VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)";
        sqlite3_stmt *st = NULL;
        if (sqlite3_prepare_v2(db, ins, -1, &st, NULL) == SQLITE_OK) {
            static char line[1024];   // task-private; a rung row is short JSON
            while (fgets(line, sizeof line, f)) {
                cJSON *r = cJSON_Parse(line);
                if (!r) continue;
                cJSON *did = cJSON_GetObjectItem(r, "device_id");
                cJSON *met = cJSON_GetObjectItem(r, "metric");
                cJSON *bs  = cJSON_GetObjectItem(r, "bucket_start");
                if (cJSON_IsString(did) && cJSON_IsString(met) && cJSON_IsNumber(bs)) {
                    sqlite3_bind_text (st, 1, res,               -1, SQLITE_STATIC);
                    sqlite3_bind_text (st, 2, did->valuestring, -1, SQLITE_STATIC);
                    sqlite3_bind_text (st, 3, met->valuestring, -1, SQLITE_STATIC);
                    sqlite3_bind_int64(st, 4, (sqlite3_int64) bs->valuedouble);
                    // vmin/vmax/vmean/vlast are nullable REAL; vcount nullable INTEGER
                    #define BIND_REAL(idx, key) do { cJSON *c = cJSON_GetObjectItem(r, key); \
                        if (cJSON_IsNumber(c)) sqlite3_bind_double(st, idx, c->valuedouble); \
                        else sqlite3_bind_null(st, idx); } while (0)
                    BIND_REAL(5, "vmin");
                    BIND_REAL(6, "vmax");
                    BIND_REAL(7, "vmean");
                    cJSON *vc = cJSON_GetObjectItem(r, "vcount");
                    if (cJSON_IsNumber(vc)) sqlite3_bind_int64(st, 8, (sqlite3_int64) vc->valuedouble);
                    else sqlite3_bind_null(st, 8);
                    BIND_REAL(9, "vlast");
                    #undef BIND_REAL
                    if (sqlite3_step(st) == SQLITE_DONE) applied++;
                    sqlite3_reset(st);
                    sqlite3_clear_bindings(st);
                }
                cJSON_Delete(r);
            }
            sqlite3_finalize(st);
        }
        rc = sqlite3_exec(db, "COMMIT", NULL, NULL, NULL);
        sqlite3_close(db);
    } else rc = SQLITE_ERROR;
    if (s_db_mutex) xSemaphoreGive(s_db_mutex);
    fclose(f);
    remove(tmp_ndjson);
    return (rc == SQLITE_OK) ? applied : -1;
}

static void sync_once(void)
{
    if (!ha_sdcard_mounted()) { ESP_LOGW(TAG, "SD not mounted — skip this cycle"); return; }
    const char *mp = ha_sdcard_mount_point();
    char db[128], tmp[160], url[240];
    static char man[2048];
    snprintf(db,  sizeof db,  "%s/%s", mp, DB_NAME);
    snprintf(tmp, sizeof tmp, "%s/%s.tmp", mp, DB_NAME);
    snprintf(s_db_path, sizeof s_db_path, "%s", db);   // publish the path for rung queries

    // 1. manifest — per-rung {rows, latest_bucket_start}; tells us which rungs are behind, cheaply.
    snprintf(url, sizeof url, "%s/api/v1/rung/manifest.json", s_base);
    if (http_get(url, man, sizeof man) <= 0) {
        ESP_LOGW(TAG, "manifest fetch failed (server rungs.db not built yet?)");
        return;
    }
    cJSON *j = cJSON_Parse(man);
    cJSON *rungs = j ? cJSON_GetObjectItem(j, "rungs") : NULL;
    if (!cJSON_IsObject(rungs)) { ESP_LOGW(TAG, "manifest missing rungs"); if (j) cJSON_Delete(j); return; }

    // 2. cold start (no local db) → one-time full.db seed via temp + atomic swap; incremental takes
    //    over on the next cycle. (Seeding the whole 1min rung over NDJSON would be far larger.)
    struct stat stt;
    bool db_present = (stat(db, &stt) == 0 && stt.st_size > 0);
    if (!db_present) {
        ESP_LOGI(TAG, "cold start — seeding via full.db");
        snprintf(url, sizeof url, "%s/api/v1/rung/full.db", s_base);
        if (http_get_to_file(url, tmp)) {
            if (s_db_mutex) xSemaphoreTake(s_db_mutex, portMAX_DELAY);
            remove(db);
            if (rename(tmp, db) == 0) s_db_ready = true; else remove(tmp);
            if (s_db_mutex) xSemaphoreGive(s_db_mutex);
            if (s_db_ready) ESP_LOGI(TAG, "seed complete -> %s", db);
        } else remove(tmp);
        cJSON_Delete(j);
        return;
    }

    // 3. incremental: per rung, if the local HWM is behind the manifest, pull /since?after=<hwm> and
    //    upsert only the delta. `after` is inclusive, so the still-open boundary bucket is refreshed.
    s_db_ready = true;
    int total = 0;
    char ndj[176];
    for (int i = 0; i < NRUNGS; i++) {
        cJSON *rg = cJSON_GetObjectItem(rungs, RUNGS[i].name);
        cJSON *hi = rg ? cJSON_GetObjectItem(rg, "latest_bucket_start") : NULL;
        if (!cJSON_IsNumber(hi)) continue;                     // server has no rows for this rung yet
        long server_hi = (long) hi->valuedouble;
        if (s_db_mutex) xSemaphoreTake(s_db_mutex, portMAX_DELAY);
        long hwm = local_hwm(RUNGS[i].name);
        if (s_db_mutex) xSemaphoreGive(s_db_mutex);
        if (hwm >= server_hi) continue;                        // this rung is up to date
        snprintf(ndj, sizeof ndj, "%s/rung_%s.ndj.tmp", mp, RUNGS[i].name);
        int n = sync_rung(RUNGS[i].name, hwm, ndj);            // after=hwm (inclusive → boundary re-sent)
        if (n > 0) { total += n; ESP_LOGI(TAG, "rung %s: +%d rows (hwm %ld -> %ld)", RUNGS[i].name, n, hwm, server_hi); }
    }
    if (total) ESP_LOGI(TAG, "incremental sync: %d rows applied", total);
    else       ESP_LOGI(TAG, "replica up to date");
    cJSON_Delete(j);
}

// ── #7 files lane: warm-standby backup of the dictator instance/ (config+parquet+hot.db) ─────────
// Contract: docs/design/instance-replica-lane.md. Whole-file-by-sha256: the server manifest lists each
// artifact's sha256; we keep the last-synced sha in PROVENANCE.json and re-download only when it changes.
// GATED on is_source_of_record so a demoted standby can't poison the backup. Lower cadence than the rung
// lane (config/parquet/hot.db move slowly). Reuses http_get / http_get_to_file.
#define FILES_POLL_EVERY 6                  // run every 6th cycle (~hourly at POLL_MS=10min)
#define HOTDB_EVERY      6                  // hot.db sub-cadence: pull it only every 6th files run (~4x/day).
                                            // hot.db is 48 MB and its sha changes every cycle, so fetching it
                                            // hourly is ~1 GB/day of needless traffic for a cold backup whose
                                            // freshness has low value (the contract wanted "a few times/day").
                                            // config + parquet still sync every files run (cheap / immutable).
#define REPLICA_DIR      "replica"
#define PROV_NAME        "PROVENANCE.json"
#define SD_FREE_FLOOR_MB 64                 // don't fetch a new artifact below this free-space floor (logged)

// flatten a possibly-slashed artifact name ("2026/07/x.parquet") into one SD filename
static void flat_name(const char *kind, const char *name, char *out, int cap)
{
    int j = snprintf(out, cap, "%s_", kind);
    for (const char *p = name; *p && j < cap - 1; p++) out[j++] = (*p == '/' || *p == ' ') ? '_' : *p;
    out[j] = '\0';
}

static long sd_free_mb(const char *mp)
{
    uint64_t total = 0, freeb = 0;
    if (esp_vfs_fat_info(mp, &total, &freeb) != ESP_OK) return -1;
    return (long)(freeb >> 20);
}

// include_hotdb=false skips the big 48 MB hot.db this run (config+parquet still sync). Scheduled runs pass
// true only every HOTDB_EVERY-th files cycle; the manual cmd/replica trigger always passes true (full pull).
static void sync_files_once(bool include_hotdb)
{
    if (!ha_sdcard_mounted()) return;
    const char *mp = ha_sdcard_mount_point();

    char url[288];
    char *man = malloc(8192);
    if (!man) return;
    snprintf(url, sizeof url, "%s/api/v1/replica/manifest.json", s_base);
    int mn = http_get(url, man, 8192);
    cJSON *j = (mn > 0) ? cJSON_ParseWithLength(man, mn) : NULL;
    free(man);
    if (!j) return;                          // route not up yet (dev half) or bad JSON → silent skip

    cJSON *sor = cJSON_GetObjectItem(j, "is_source_of_record");
    if (!cJSON_IsBool(sor) || !cJSON_IsTrue(sor)) {   // ADR-0018 gate: keep the good copy, don't overwrite
        ESP_LOGW(TAG, "files lane: server not source-of-record — skipping (backup preserved)");
        cJSON_Delete(j); return;
    }
    cJSON *arts = cJSON_GetObjectItem(j, "artifacts");
    if (!cJSON_IsArray(arts)) { cJSON_Delete(j); return; }
    const cJSON *stag = cJSON_GetObjectItem(j, "source_tag");
    const cJSON *gts  = cJSON_GetObjectItem(j, "generated_ts");

    char dir[160]; snprintf(dir, sizeof dir, "%s/%s", mp, REPLICA_DIR);
    mkdir(dir, 0777);

    // load (or start) provenance: { source_tag, generated_ts, sha: { "<kind>/<name>": "<sha256>" } }
    char provpath[224]; snprintf(provpath, sizeof provpath, "%s/%s", dir, PROV_NAME);
    cJSON *prov = NULL;
    char *pbuf = malloc(4096);
    if (pbuf) { int pn = 0; FILE *pf = fopen(provpath, "r");
        if (pf) { pn = (int)fread(pbuf, 1, 4095, pf); fclose(pf); pbuf[pn > 0 ? pn : 0] = '\0'; }
        if (pn > 0) prov = cJSON_Parse(pbuf);
        free(pbuf);
    }
    if (!prov) prov = cJSON_CreateObject();
    cJSON *sha = cJSON_GetObjectItem(prov, "sha");
    if (!cJSON_IsObject(sha)) { sha = cJSON_AddObjectToObject(prov, "sha"); }

    int fetched = 0, skipped = 0;
    const cJSON *a;
    cJSON_ArrayForEach(a, arts) {
        const cJSON *k = cJSON_GetObjectItem(a, "kind");
        const cJSON *nm = cJSON_GetObjectItem(a, "name");
        const cJSON *sh = cJSON_GetObjectItem(a, "sha256");
        if (!cJSON_IsString(k) || !cJSON_IsString(nm) || !cJSON_IsString(sh)) continue;
        if (!include_hotdb && strcmp(k->valuestring, "hotdb") == 0) continue;   // sub-cadence: skip hot.db this run
        char key[256]; snprintf(key, sizeof key, "%s/%s", k->valuestring, nm->valuestring);
        const cJSON *have = cJSON_GetObjectItem(sha, key);
        if (cJSON_IsString(have) && strcmp(have->valuestring, sh->valuestring) == 0) continue;  // current
        if (sd_free_mb(mp) >= 0 && sd_free_mb(mp) < SD_FREE_FLOOR_MB) {
            ESP_LOGW(TAG, "files lane: SD below %d MB free — SKIP %s (not silently dropped)", SD_FREE_FLOOR_MB, key);
            skipped++; continue;             // no silent truncation — logged; LRU parquet prune = follow-up
        }
        char fn[256]; flat_name(k->valuestring, nm->valuestring, fn, sizeof fn);
        char fpath[420], tmp[440], furl[700];
        snprintf(fpath, sizeof fpath, "%s/%s", dir, fn);
        snprintf(tmp,  sizeof tmp,  "%s.tmp", fpath);
        snprintf(furl, sizeof furl, "%s/api/v1/replica/file?kind=%s&name=%s", s_base, k->valuestring, nm->valuestring);
        if (http_get_to_file(furl, tmp) && rename(tmp, fpath) == 0) {
            cJSON_DeleteItemFromObject(sha, key);
            cJSON_AddStringToObject(sha, key, sh->valuestring);
            fetched++;
        } else { remove(tmp); ESP_LOGW(TAG, "files lane: fetch failed %s", key); }
    }

    // record provenance (what/where/when) so a restore knows the origin — atomic temp+rename
    if (cJSON_IsString(stag)) { cJSON_DeleteItemFromObject(prov, "source_tag");
        cJSON_AddStringToObject(prov, "source_tag", stag->valuestring); }
    if (cJSON_IsString(gts))  { cJSON_DeleteItemFromObject(prov, "generated_ts");
        cJSON_AddStringToObject(prov, "generated_ts", gts->valuestring); }
    char *ptxt = cJSON_PrintUnformatted(prov);
    if (ptxt) {
        char ptmp[240]; snprintf(ptmp, sizeof ptmp, "%s.tmp", provpath);
        FILE *pf = fopen(ptmp, "w");
        if (pf) { fwrite(ptxt, 1, strlen(ptxt), pf); fclose(pf); rename(ptmp, provpath); }
        cJSON_free(ptxt);
    }
    if (fetched || skipped)
        ESP_LOGI(TAG, "files lane: %d fetched, %d skipped from %s", fetched, skipped,
                 cJSON_IsString(stag) ? stag->valuestring : "?");
    cJSON_Delete(prov);
    cJSON_Delete(j);
}

static volatile bool s_files_busy;      // best-effort guard so a manual trigger can't overlap the scheduled run

static void replica_task(void *pv)
{
    vTaskDelay(pdMS_TO_TICKS(BOOT_MS));
    int cycle = 0, files_cycle = 0;
    for (;;) {
        sync_once();                                        // rung ladder (ADR-0022)
        if (cycle % FILES_POLL_EVERY == 0 && !s_files_busy) {   // #7 instance/ backup lane
            s_files_busy = true;
            sync_files_once(files_cycle % HOTDB_EVERY == 0);    // hot.db only every HOTDB_EVERY-th files run
            files_cycle++;
            s_files_busy = false;
        }
        cycle++;
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    }
}

// One-shot files-lane trigger (cmd/replica): run the #7 backup pull NOW instead of waiting for the ~hourly
// cycle. Runs on its own task — sync_files_once does blocking HTTP, which must never sit on the mqtt-callback
// stack (the v11/v17 lesson). Guarded against overlapping the scheduled run.
static void files_trigger_task(void *pv)
{
    if (!s_files_busy) { s_files_busy = true; sync_files_once(true); s_files_busy = false; }  // manual = full (incl hot.db)
    else ESP_LOGW(TAG, "files lane: sync already running — trigger ignored");
    vTaskDelete(NULL);
}

void ha_replica_sync_files_now(void)
{
    xTaskCreate(files_trigger_task, "replfiles", 8192, NULL, 4, NULL);
}

void ha_replica_start(const char *base)
{
    snprintf(s_base, sizeof s_base, "%s", base);
    if (!s_db_mutex) s_db_mutex = xSemaphoreCreateMutex();
    xTaskCreate(replica_task, "ha_replica", 8192, NULL, 3, NULL);
    ESP_LOGI(TAG, "ha_replica start -> %s", s_base);
}

void ha_replica_sd_removed(void)
{
    if (s_db_mutex) xSemaphoreTake(s_db_mutex, portMAX_DELAY);
    s_db_ready = false;          // queries now fall back to the network
    s_db_path[0] = '\0';
    if (s_db_mutex) xSemaphoreGive(s_db_mutex);
    ESP_LOGW(TAG, "SD removed — local replica offline (queries fall back to network)");
}

void ha_replica_sd_inserted(void)
{
    if (!ha_sdcard_mounted()) return;
    if (s_db_mutex) xSemaphoreTake(s_db_mutex, portMAX_DELAY);
    snprintf(s_db_path, sizeof s_db_path, "%s/%s", ha_sdcard_mount_point(), DB_NAME);
    struct stat st;
    if (stat(s_db_path, &st) == 0 && st.st_size > 0) {
        s_db_ready = true;       // usable immediately; the sync task freshens it on its next cycle
        ESP_LOGW(TAG, "SD inserted — replica inventory: %s present (%ld KB)",
                 s_db_path, (long)(st.st_size >> 10));
    } else {
        s_db_ready = false;
        ESP_LOGW(TAG, "SD inserted — no replica yet; sync will pull on next cycle");
    }
    if (s_db_mutex) xSemaphoreGive(s_db_mutex);
}

int ha_replica_rung_query(const char *device_id, const char *metric, int hours, double *out, int cap)
{
    if (!s_db_ready || !s_db_mutex || !s_db_path[0]) return -1;
    long span = (long)hours * 3600;
    if (xSemaphoreTake(s_db_mutex, pdMS_TO_TICKS(300)) != pdTRUE) return -1;   // busy → let caller fall back

    int n = 0;
    sqlite3 *db = NULL;
    if (sqlite3_open_v2(s_db_path, &db, SQLITE_OPEN_READONLY, NULL) == SQLITE_OK) {
        // PK is (res,device_id,metric,bucket_start) WITHOUT ROWID → a covering scan of the btree tail
        // (fast: ~9.6 ms warm on this P4). DESC+LIMIT = the most recent N.
        static const char *q =
            "SELECT vmean FROM rung WHERE res=?1 AND device_id=?2 AND metric=?3 AND vmean IS NOT NULL "
            "ORDER BY bucket_start DESC LIMIT ?4";
        // Escalate coarser on an EMPTY rung: the server keeps 1min only ~7d, so an older window that
        // resolves to 1min finds nothing — fall through 1min→1hour→1day→1week (the panel has no raw
        // fallback the server has; per dev's rollup-ladder-server note).
        for (int i = start_rung(span); i < NRUNGS && n == 0; i++) {
            int limit = (int)(span / RUNGS[i].secs);
            if (limit < 1) limit = 1;
            if (limit > cap) limit = cap;
            sqlite3_stmt *st = NULL;
            if (sqlite3_prepare_v2(db, q, -1, &st, NULL) == SQLITE_OK) {
                sqlite3_bind_text(st, 1, RUNGS[i].name, -1, SQLITE_STATIC);
                sqlite3_bind_text(st, 2, device_id,    -1, SQLITE_STATIC);
                sqlite3_bind_text(st, 3, metric,       -1, SQLITE_STATIC);
                sqlite3_bind_int (st, 4, limit);
                int m = 0;
                while (m < cap && sqlite3_step(st) == SQLITE_ROW) out[m++] = sqlite3_column_double(st, 0);
                sqlite3_finalize(st);
                if (m > 0) {
                    for (int k = 0; k < m / 2; k++) { double t = out[k]; out[k] = out[m-1-k]; out[m-1-k] = t; }
                    n = m;   // oldest→newest; first non-empty rung wins
                }
            }
        }
        sqlite3_close(db);
    }
    xSemaphoreGive(s_db_mutex);
    return n;
}
