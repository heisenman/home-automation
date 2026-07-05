// ha_cmd — signed-directive verification (ADR-0010). See ha_cmd.h. Ported verbatim in behaviour from the
// live edge-node verifier (edge/esp32c6/main/ha_mqtt.c): same HMAC-over-literal-p, same constant-time
// compare, same freshness windows, same NVS "ha_cmd" (ts,seq) monotonic high-water mark. Reuse, not
// reinvent — the scheme is proven on the C6/S3 (29 unit/loop tests + a live bad-sig demo, ADR-0010).
#include "ha_cmd.h"
#include <string.h>
#include <stdio.h>
#include <time.h>
#include "mbedtls/md.h"
#include "nvs.h"

// HMAC-SHA256(secret, p) == sig_hex ? — verifies the signature over the LITERAL p string the server
// signed (cJSON hands it back un-escaped, a faithful round-trip: no canonicalisation mismatch).
static bool sig_ok(const char *p, const char *sig_hex, const char *secret)
{
    if (!secret || !secret[0] || strlen(sig_hex) != 64) return false;
    const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    if (!info) return false;
    unsigned char mac[32];
    if (mbedtls_md_hmac(info, (const unsigned char *)secret, strlen(secret),
                        (const unsigned char *)p, strlen(p), mac) != 0) return false;
    char hex[65];
    for (int i = 0; i < 32; i++) snprintf(hex + i * 2, 3, "%02x", mac[i]);
    unsigned char diff = 0;                       // constant-time compare
    for (int i = 0; i < 64; i++) diff |= (unsigned char)(hex[i] ^ sig_hex[i]);
    return diff == 0;
}

// Per-device monotonic (ts,seq) anti-replay high-water mark, persisted so a reboot can't reopen the
// window. ts is SERVER-stamped (monotonic regardless of this device's clock drift); seq orders commands
// within one second. Shared across all authority ops on this device.
static long s_repl_ts  = -1;
static int  s_repl_seq = -1;
static bool s_repl_loaded;

static void replay_load(void)
{
    if (s_repl_loaded) return;
    s_repl_loaded = true;
    nvs_handle_t h;
    if (nvs_open("ha_cmd", NVS_READONLY, &h) != ESP_OK) return;   // namespace absent on first boot
    int64_t v; int32_t q;
    if (nvs_get_i64(h, "last_ts",  &v) == ESP_OK) s_repl_ts  = (long)v;
    if (nvs_get_i32(h, "last_seq", &q) == ESP_OK) s_repl_seq = (int)q;
    nvs_close(h);
}

static void replay_store(long ts, int seq)
{
    nvs_handle_t h;
    if (nvs_open("ha_cmd", NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_i64(h, "last_ts",  (int64_t)ts);
    nvs_set_i32(h, "last_seq", (int32_t)seq);
    nvs_commit(h);
    nvs_close(h);
}

ha_cmd_result_t ha_cmd_verify(const char *payload, int len, const char *secret, cJSON **inner_out)
{
    if (inner_out) *inner_out = NULL;
    cJSON *root = cJSON_ParseWithLength(payload, len);
    if (!root) return HA_CMD_BAD_JSON;

    const cJSON *p = cJSON_GetObjectItem(root, "p");
    const cJSON *s = cJSON_GetObjectItem(root, "s");
    if (!cJSON_IsString(p) || !cJSON_IsString(s)) { cJSON_Delete(root); return HA_CMD_UNSIGNED; }
    if (!secret || !secret[0]) { cJSON_Delete(root); return HA_CMD_NO_SECRET; }
    if (!sig_ok(p->valuestring, s->valuestring, secret)) { cJSON_Delete(root); return HA_CMD_BAD_SIG; }

    cJSON *inner = cJSON_Parse(p->valuestring);
    if (!inner) { cJSON_Delete(root); return HA_CMD_BAD_JSON; }

    // Freshness window: tight for actuation, WIDE for ota so a clock-drifted device stays OTA-recoverable
    // (the image is signed-hash-verified + anti-downgrade, so a replay just re-flashes the same image).
    const cJSON *op = cJSON_GetObjectItem(inner, "op");
    long window = (cJSON_IsString(op) && strcmp(op->valuestring, "ota") == 0) ? 86400 : 300;
    const cJSON *ts = cJSON_GetObjectItem(inner, "ts");
    if (cJSON_IsNumber(ts)) {
        long mts = (long)ts->valuedouble;
        long dt = (long)time(NULL) - mts;
        if (dt < -window || dt > window) { cJSON_Delete(inner); cJSON_Delete(root); return HA_CMD_STALE; }
        // Monotonic (ts,seq) anti-replay: refuse anything not STRICTLY newer than the last we acted on.
        const cJSON *sq = cJSON_GetObjectItem(inner, "seq");
        int mseq = cJSON_IsNumber(sq) ? (int)sq->valuedouble : 0;
        replay_load();
        if (s_repl_ts >= 0 && (mts < s_repl_ts || (mts == s_repl_ts && mseq <= s_repl_seq))) {
            cJSON_Delete(inner); cJSON_Delete(root); return HA_CMD_REPLAY;
        }
        s_repl_ts = mts; s_repl_seq = mseq; replay_store(mts, mseq);
    }
    // else: no ts -> no freshness/replay gate (matches the edge node; a signed op without ts still acts).

    cJSON_Delete(root);
    if (inner_out) *inner_out = inner; else cJSON_Delete(inner);
    return HA_CMD_OK;
}

const char *ha_cmd_result_str(ha_cmd_result_t r)
{
    switch (r) {
        case HA_CMD_OK:        return "ok";
        case HA_CMD_BAD_JSON:  return "bad-json";
        case HA_CMD_UNSIGNED:  return "unsigned";
        case HA_CMD_NO_SECRET: return "no-secret";
        case HA_CMD_BAD_SIG:   return "bad-sig";
        case HA_CMD_STALE:     return "stale";
        case HA_CMD_REPLAY:    return "replay";
        default:               return "?";
    }
}
