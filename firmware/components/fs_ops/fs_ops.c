// SD file-ops over MQTT — see fs_ops.h.
#include "fs_ops.h"

#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <dirent.h>
#include <sys/stat.h>
#include <unistd.h>
#include <errno.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "esp_vfs_fat.h"
#include "cJSON.h"
#include "mbedtls/base64.h"

static const char *TAG = "fsops";

#define SD_ROOT     "/sdcard"
#define READ_CAP    1536          // max bytes per read (base64 ~2 KB — fits an MQTT payload)
#define WRITE_CAP   4096          // max decoded bytes per write
#define LS_CAP      128           // max dir entries per ls
#define Q_DEPTH     6

static QueueHandle_t s_q;
static void (*s_publish)(const char *json);

// Reject anything not under the SD mount so a stray rm can't hit spiffs/nvs/other VFS roots.
static bool path_ok(const char *p)
{
    if (!p) return false;
    if (strncmp(p, SD_ROOT, strlen(SD_ROOT)) != 0) return false;
    if (strstr(p, "/../")) return false;               // no traversal
    return true;
}

static void emit(cJSON *resp)
{
    char *out = cJSON_PrintUnformatted(resp);
    if (out) {
        if (s_publish) s_publish(out);
        cJSON_free(out);
    }
    cJSON_Delete(resp);
}

// Build a response object pre-seeded with op/path/id echoed from the command.
static cJSON *resp_for(const cJSON *cmd, const char *op, const char *path)
{
    cJSON *r = cJSON_CreateObject();
    if (op)   cJSON_AddStringToObject(r, "op", op);
    if (path) cJSON_AddStringToObject(r, "path", path);
    const cJSON *id = cJSON_GetObjectItem(cmd, "id");
    if (cJSON_IsString(id)) cJSON_AddStringToObject(r, "id", id->valuestring);
    else if (cJSON_IsNumber(id)) cJSON_AddNumberToObject(r, "id", id->valuedouble);
    return r;
}

static void fail(const cJSON *cmd, const char *op, const char *path, const char *err)
{
    cJSON *r = resp_for(cmd, op, path);
    cJSON_AddBoolToObject(r, "ok", false);
    cJSON_AddStringToObject(r, "err", err);
    emit(r);
}

static void do_ls(const cJSON *cmd, const char *path)
{
    DIR *d = opendir(path);
    if (!d) { fail(cmd, "ls", path, strerror(errno)); return; }
    cJSON *r = resp_for(cmd, "ls", path);
    cJSON_AddBoolToObject(r, "ok", true);
    cJSON *arr = cJSON_AddArrayToObject(r, "entries");
    struct dirent *e; int n = 0;
    char full[320];
    while (n < LS_CAP && (e = readdir(d))) {
        snprintf(full, sizeof(full), "%s/%s", path, e->d_name);
        struct stat st; cJSON *o = cJSON_CreateObject();
        cJSON_AddStringToObject(o, "name", e->d_name);
        if (stat(full, &st) == 0) {
            cJSON_AddNumberToObject(o, "size", (double)st.st_size);
            cJSON_AddBoolToObject(o, "dir", S_ISDIR(st.st_mode));
        }
        cJSON_AddItemToArray(arr, o);
        n++;
    }
    closedir(d);
    cJSON_AddNumberToObject(r, "count", n);
    emit(r);
}

static void do_stat(const cJSON *cmd, const char *path)
{
    struct stat st;
    cJSON *r = resp_for(cmd, "stat", path);
    if (stat(path, &st) != 0) {
        cJSON_AddBoolToObject(r, "ok", true);
        cJSON_AddBoolToObject(r, "exists", false);
    } else {
        cJSON_AddBoolToObject(r, "ok", true);
        cJSON_AddBoolToObject(r, "exists", true);
        cJSON_AddNumberToObject(r, "size", (double)st.st_size);
        cJSON_AddBoolToObject(r, "dir", S_ISDIR(st.st_mode));
    }
    emit(r);
}

static void do_read(const cJSON *cmd, const char *path)
{
    long off = 0; int len = READ_CAP;
    const cJSON *jo = cJSON_GetObjectItem(cmd, "off");
    const cJSON *jl = cJSON_GetObjectItem(cmd, "len");
    if (cJSON_IsNumber(jo)) off = (long)jo->valuedouble;
    if (cJSON_IsNumber(jl)) len = (int)jl->valuedouble;
    if (len < 0 || len > READ_CAP) len = READ_CAP;

    FILE *f = fopen(path, "rb");
    if (!f) { fail(cmd, "read", path, strerror(errno)); return; }
    struct stat st; long size = (fstat(fileno(f), &st) == 0) ? st.st_size : -1;
    if (off > 0) fseek(f, off, SEEK_SET);
    unsigned char *buf = malloc(len);
    if (!buf) { fclose(f); fail(cmd, "read", path, "oom"); return; }
    size_t got = fread(buf, 1, len, f);
    int eof = feof(f) ? 1 : 0;
    fclose(f);

    size_t b64cap = ((got + 2) / 3) * 4 + 4, olen = 0;
    char *b64 = malloc(b64cap);
    if (!b64) { free(buf); fail(cmd, "read", path, "oom"); return; }
    mbedtls_base64_encode((unsigned char *)b64, b64cap, &olen, buf, got);
    b64[olen] = 0;
    free(buf);

    cJSON *r = resp_for(cmd, "read", path);
    cJSON_AddBoolToObject(r, "ok", true);
    cJSON_AddNumberToObject(r, "off", (double)off);
    cJSON_AddNumberToObject(r, "n", (double)got);
    cJSON_AddNumberToObject(r, "size", (double)size);
    cJSON_AddBoolToObject(r, "eof", eof);
    cJSON_AddStringToObject(r, "b64", b64);
    free(b64);
    emit(r);
}

static void do_write(const cJSON *cmd, const char *path)
{
    bool append = cJSON_IsTrue(cJSON_GetObjectItem(cmd, "append"));
    const cJSON *jt = cJSON_GetObjectItem(cmd, "text");
    const cJSON *jb = cJSON_GetObjectItem(cmd, "b64");
    unsigned char *data = NULL; size_t dlen = 0; bool owned = false;

    if (cJSON_IsString(jt)) {
        data = (unsigned char *)jt->valuestring;
        dlen = strlen(jt->valuestring);
    } else if (cJSON_IsString(jb)) {
        size_t src = strlen(jb->valuestring), cap = (src / 4) * 3 + 4;
        data = malloc(cap); owned = true;
        if (!data) { fail(cmd, "write", path, "oom"); return; }
        if (mbedtls_base64_decode(data, cap, &dlen, (const unsigned char *)jb->valuestring, src) != 0) {
            free(data); fail(cmd, "write", path, "bad-b64"); return;
        }
    } else { fail(cmd, "write", path, "need text|b64"); return; }

    if (dlen > WRITE_CAP) { if (owned) free(data); fail(cmd, "write", path, "too-big"); return; }

    FILE *f = fopen(path, append ? "ab" : "wb");
    if (!f) { if (owned) free(data); fail(cmd, "write", path, strerror(errno)); return; }
    size_t wrote = fwrite(data, 1, dlen, f);
    fflush(f); fsync(fileno(f));
    fclose(f);
    if (owned) free(data);

    cJSON *r = resp_for(cmd, "write", path);
    cJSON_AddBoolToObject(r, "ok", wrote == dlen);
    cJSON_AddNumberToObject(r, "n", (double)wrote);
    cJSON_AddBoolToObject(r, "append", append);
    emit(r);
}

static void do_rm(const cJSON *cmd, const char *path)
{
    int rc = remove(path);
    if (rc != 0) rc = rmdir(path);          // remove() may not handle dirs on FATFS
    cJSON *r = resp_for(cmd, "rm", path);
    cJSON_AddBoolToObject(r, "ok", rc == 0);
    if (rc != 0) cJSON_AddStringToObject(r, "err", strerror(errno));
    emit(r);
}

static void do_mkdir(const cJSON *cmd, const char *path)
{
    int rc = mkdir(path, 0777);
    cJSON *r = resp_for(cmd, "mkdir", path);
    cJSON_AddBoolToObject(r, "ok", rc == 0 || errno == EEXIST);
    if (rc != 0 && errno != EEXIST) cJSON_AddStringToObject(r, "err", strerror(errno));
    emit(r);
}

static void do_df(const cJSON *cmd)
{
    uint64_t total = 0, freeb = 0;
    cJSON *r = resp_for(cmd, "df", SD_ROOT);
    esp_err_t e = esp_vfs_fat_info(SD_ROOT, &total, &freeb);
    if (e != ESP_OK) {
        cJSON_AddBoolToObject(r, "ok", false);
        cJSON_AddStringToObject(r, "err", esp_err_to_name(e));
    } else {
        cJSON_AddBoolToObject(r, "ok", true);
        cJSON_AddNumberToObject(r, "total", (double)total);
        cJSON_AddNumberToObject(r, "free", (double)freeb);
    }
    emit(r);
}

static void handle(const char *json)
{
    cJSON *cmd = cJSON_Parse(json);
    if (!cmd) { ESP_LOGW(TAG, "bad json"); return; }
    const cJSON *jop = cJSON_GetObjectItem(cmd, "op");
    const cJSON *jpath = cJSON_GetObjectItem(cmd, "path");
    const char *op = cJSON_IsString(jop) ? jop->valuestring : "";
    const char *path = cJSON_IsString(jpath) ? jpath->valuestring : NULL;

    if (strcmp(op, "df") == 0) { do_df(cmd); cJSON_Delete(cmd); return; }
    if (strcmp(op, "ls") == 0 && !path) path = SD_ROOT;   // ls defaults to the SD root

    if (!path_ok(path)) { fail(cmd, op, path ? path : "", "path must be under /sdcard"); cJSON_Delete(cmd); return; }

    if      (strcmp(op, "ls")    == 0) do_ls(cmd, path);
    else if (strcmp(op, "stat")  == 0) do_stat(cmd, path);
    else if (strcmp(op, "read")  == 0) do_read(cmd, path);
    else if (strcmp(op, "write") == 0) do_write(cmd, path);
    else if (strcmp(op, "rm")    == 0) do_rm(cmd, path);
    else if (strcmp(op, "mkdir") == 0) do_mkdir(cmd, path);
    else fail(cmd, op, path, "unknown op");
    cJSON_Delete(cmd);
}

static void fs_task(void *pv)
{
    char *json;
    for (;;) {
        if (xQueueReceive(s_q, &json, portMAX_DELAY) == pdTRUE) {
            handle(json);
            free(json);
        }
    }
}

void fs_ops_submit(const char *json_cmd)
{
    if (!s_q || !json_cmd) return;
    char *copy = strdup(json_cmd);
    if (!copy) return;
    if (xQueueSend(s_q, &copy, 0) != pdTRUE) {   // full → drop, don't block the mqtt stack
        ESP_LOGW(TAG, "fs queue full, dropped");
        free(copy);
    }
}

esp_err_t fs_ops_start(void (*publish)(const char *json))
{
    s_publish = publish;
    s_q = xQueueCreate(Q_DEPTH, sizeof(char *));
    if (!s_q) return ESP_ERR_NO_MEM;
    if (xTaskCreate(fs_task, "fsops", 6144, NULL, 3, NULL) != pdPASS) return ESP_FAIL;
    return ESP_OK;
}
