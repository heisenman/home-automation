// Shared edge MQTT client (ADR-0010/0023) — see ha_mqtt.h. Promoted from edge/esp32c6/main/ha_mqtt.c; the
// only changes vs the fork are the board seam (secrets + hooks + reach flag arrive via ha_mqtt_init instead
// of secrets.h #defines) and the LED status hooks (cfg callbacks). Core — HMAC verify, monotonic anti-replay,
// dispatch, publish paths, event loop — is unchanged. The gatt/ota/gatt_exec platform seams are wired
// internally here (they call this component's own publish/log), so they're no longer per-board.
#include "ha_mqtt.h"
#include "ha_sntp.h"
#include "ha_gatt.h"
#include "ha_gatt_exec.h"
#include "ha_ota.h"
#include "ha_relay.h"
#include "ha_reach.h"
#include "ha_config.h"        // ha_config_repoint_apply (the signed "repoint" op)
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include "mqtt_client.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_chip_info.h"      // ADR-0036 hello: chip model → "esp32c6"/"esp32s3"/…
#include "esp_mac.h"            // ADR-0036 hello: base MAC for the self-describing identity doc
#include "cJSON.h"
#include <time.h>
#include "mbedtls/md.h"
#include "nvs.h"
#include "ha_ble_scan.h"        // ha_ble_scan_pause/resume — wired into the shared ha_ota radio seam

static const char *TAG = "ha_mqtt";
static ha_mqtt_cfg_t s_cfg;     // board seam (secrets, flags, hooks) — installed by ha_mqtt_init
static esp_mqtt_client_handle_t s_client;
static char s_node[32];
static char s_status_topic[64];
static char s_cmd_topic[64];
static char s_relay_topic[64];        // home/edge/<node>/relay — signed Phase-B coverage directives
static char s_reach_req_topic[64];    // home/edge/<node>/reach/req — signed census trigger (ADR-0023)
static char s_online_msg[64];     // "online <slot> <fwver>" — shows which OTA slot/version is running
static char s_hello_topic[64];    // home/edge/<node>/hello — ADR-0036 retained self-describing identity
static char s_hello_msg[256];     // the identity JSON, built once at start, published retained on connect
static volatile bool s_connected;

void ha_mqtt_init(const ha_mqtt_cfg_t *cfg) { if (cfg) s_cfg = *cfg; }
bool ha_mqtt_has_cmd_secret(void) { return s_cfg.cmd_secret && s_cfg.cmd_secret[0]; }

// lower-case, colon-stripped MAC into dst (>=13 bytes)
static void macflat(const char *mac_str, char *dst) {
    int j = 0;
    for (const char *p = mac_str; *p && j < 12; ++p) {
        if (*p == ':') continue;
        char c = *p;
        dst[j++] = (c >= 'A' && c <= 'Z') ? (c + 32) : c;
    }
    dst[j] = '\0';
}

// HMAC-SHA256(secret, p) == sig_hex ?  Verifies a signed-envelope command (ADR-0010). We hash the
// LITERAL p string the server signed (cJSON returns it un-escaped, a faithful round-trip), so there
// is no canonicalisation mismatch between the Python signer and this verifier.
static bool cmd_sig_ok(const char *p, const char *sig_hex) {
    const char *secret = s_cfg.cmd_secret;
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

// Per-node monotonic (ts,seq) anti-replay (ADR-0010). We act on a signed command only if its (ts,seq)
// is STRICTLY greater than the last one we acted on, and persist the high-water mark in NVS so a reboot
// can't reopen the replay window. ts is SERVER-stamped, so the comparison is monotonic regardless of
// THIS node's clock drift (the freshness window is the only clock-sensitive gate) — and it self-heals
// after a dictator rebuild because wall-clock only advances. seq orders commands within one second.
static long s_repl_ts  = -1;
static int  s_repl_seq = -1;
static bool s_repl_loaded;
static void replay_load(void) {
    if (s_repl_loaded) return;
    s_repl_loaded = true;
    nvs_handle_t h;
    if (nvs_open("ha_cmd", NVS_READONLY, &h) != ESP_OK) return;   // namespace absent on first boot
    int64_t v; int32_t q;
    if (nvs_get_i64(h, "last_ts",  &v) == ESP_OK) s_repl_ts  = (long)v;
    if (nvs_get_i32(h, "last_seq", &q) == ESP_OK) s_repl_seq = (int)q;
    nvs_close(h);
}
static void replay_store(long ts, int seq) {
    nvs_handle_t h;
    if (nvs_open("ha_cmd", NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_i64(h, "last_ts",  (int64_t)ts);
    nvs_set_i32(h, "last_seq", (int32_t)seq);
    nvs_commit(h);
    nvs_close(h);
}

// Dispatch a verified command object (op/mac/steps/url). Caller owns the cJSON.
static void dispatch_cmd(const cJSON *cmd) {
    const cJSON *op = cJSON_GetObjectItem(cmd, "op");
    const cJSON *mac = cJSON_GetObjectItem(cmd, "mac");
    const cJSON *prof = cJSON_GetObjectItem(cmd, "profile");
    if (cJSON_IsString(op) && strcmp(op->valuestring, "history") == 0 && cJSON_IsString(mac)) {
        const char *profile = cJSON_IsString(prof) ? prof->valuestring : "outdoor";
        const cJSON *win = cJSON_GetObjectItem(cmd, "window");   // optional: records back from the write ptr
        int window = cJSON_IsNumber(win) ? win->valueint : 0;    // 0 = profile default
        ESP_LOGI(TAG, "cmd: history pull mac=%s profile=%s window=%d", mac->valuestring, profile, window);
        if (ha_gatt_exec_busy()) ESP_LOGW(TAG, "central busy; dropping history pull");
        else ha_gatt_history_pull(mac->valuestring, profile, window);
    } else if (cJSON_IsString(op) && strcmp(op->valuestring, "gatt") == 0 && cJSON_IsString(mac)) {
        // Generic GATT forwarder: {"op":"gatt","reqid":"..","mac":"..","steps":[...]}
        const cJSON *reqid = cJSON_GetObjectItem(cmd, "reqid");
        const cJSON *steps = cJSON_GetObjectItem(cmd, "steps");
        if (!cJSON_IsArray(steps)) { ESP_LOGW(TAG, "gatt cmd: missing steps[]"); return; }
        char *steps_json = cJSON_PrintUnformatted(steps);   // re-serialise just the steps array
        const char *rid = cJSON_IsString(reqid) ? reqid->valuestring : "0";
        ESP_LOGI(TAG, "cmd: gatt exec mac=%s reqid=%s", mac->valuestring, rid);
        if (ha_gatt_busy() || ha_gatt_exec_busy()) ESP_LOGW(TAG, "central busy; dropping gatt exec");
        else if (steps_json) ha_gatt_exec_run(rid, mac->valuestring, steps_json);
        if (steps_json) cJSON_free(steps_json);
    } else if (cJSON_IsString(op) && strcmp(op->valuestring, "ota") == 0) {
        // Firmware OTA: {"op":"ota","url":"http://<server>:<port>/ha-edge-<board>.bin"}
        const cJSON *url = cJSON_GetObjectItem(cmd, "url");
        const cJSON *sha = cJSON_GetObjectItem(cmd, "sha256");
        if (cJSON_IsString(url)) {
            ESP_LOGI(TAG, "cmd: ota url=%s", url->valuestring);
            ha_ota_start(url->valuestring, cJSON_IsString(sha) ? sha->valuestring : NULL);
        } else ESP_LOGW(TAG, "ota cmd: missing url");
    } else if (cJSON_IsString(op) && strcmp(op->valuestring, "repoint") == 0) {
        // Air-gap migration (DJ-19): {"op":"repoint","ssid":..,"psk":..,"broker":"mqtt://host:1883",
        // "ntp":..,"ota_host":..}. Rewrites the "ha" overlay + reboots onto the new net; a boot-count
        // revert self-heals a bad move over the air. Rides the same signed/fresh/replay gate as every op.
        const cJSON *ssid   = cJSON_GetObjectItem(cmd, "ssid");
        const cJSON *psk    = cJSON_GetObjectItem(cmd, "psk");
        const cJSON *broker = cJSON_GetObjectItem(cmd, "broker");
        const cJSON *ntp    = cJSON_GetObjectItem(cmd, "ntp");
        const cJSON *host   = cJSON_GetObjectItem(cmd, "ota_host");
        if (cJSON_IsString(ssid) && cJSON_IsString(psk) && cJSON_IsString(broker)) {
            ha_mqtt_log("cmd: repoint ssid=%s broker=%s — arming + reboot", ssid->valuestring, broker->valuestring);
            ha_config_repoint_apply(ssid->valuestring, psk->valuestring, broker->valuestring,
                                    cJSON_IsString(ntp)  ? ntp->valuestring  : NULL,
                                    cJSON_IsString(host) ? host->valuestring : NULL);
            ha_mqtt_log("repoint apply returned — NVS write failed, node not moved");  // apply reboots on success
        } else ha_mqtt_log("repoint cmd: missing ssid/psk/broker");
    } else if (cJSON_IsString(op) && strcmp(op->valuestring, "batt_refresh") == 0) {
        // Battery refresh: {"op":"batt_refresh","window_s":8,"mac":<optional,informational>}. Opens a
        // brief ACTIVE-scan window so SwitchBot meters' fd3d scan-responses (carrying battery%) are
        // captured; the per-MAC cache then backfills the passive temp/hum stream. Deliberately rare +
        // short (nightly job + PWA on-demand) so the fleet stays passive and RF-quiet the rest of the day.
        const cJSON *win = cJSON_GetObjectItem(cmd, "window_s");
        int secs = cJSON_IsNumber(win) ? (int)win->valuedouble : 8;
        ESP_LOGI(TAG, "cmd: batt_refresh window=%ds", secs);
        ha_ble_scan_active_window(secs);
    } else {
        ESP_LOGW(TAG, "unknown/!malformed cmd");
    }
}

static void handle_cmd(const char *data, int len) {
    cJSON *root = cJSON_ParseWithLength(data, len);
    if (!root) { ESP_LOGW(TAG, "bad cmd json"); return; }

    cJSON *inner = NULL;
    const cJSON *cmd = root;
    const cJSON *p = cJSON_GetObjectItem(root, "p");
    const cJSON *s = cJSON_GetObjectItem(root, "s");
    if (cJSON_IsString(p) && cJSON_IsString(s)) {
        // Signed envelope {p,s}: verify HMAC over the literal p string, then act on the inner cmd.
        if (!cmd_sig_ok(p->valuestring, s->valuestring)) {
            ha_mqtt_log("cmd rejected: bad-sig"); cJSON_Delete(root); return;
        }
        inner = cJSON_Parse(p->valuestring);
        if (!inner) { ESP_LOGW(TAG, "cmd: bad inner json"); cJSON_Delete(root); return; }
        // Freshness window: tight for actuation/gatt (replay defense), but WIDE for ota so a node whose
        // clock has drifted (the C6 RTC does) can still be OTA-recovered — the OTA directive is signed +
        // image-hash-verified + version anti-downgrade, so a replay just re-flashes the same image.
        const cJSON *op = cJSON_GetObjectItem(inner, "op");
        // Wide freshness window for ota (clock-drift recovery) and batt_refresh (benign + idempotent: a
        // replay just re-opens a harmless active-scan window, and the C6 RTC drifts between nightly runs).
        long window = (cJSON_IsString(op) && (strcmp(op->valuestring, "ota") == 0
                                           || strcmp(op->valuestring, "batt_refresh") == 0)) ? 86400 : 300;
        const cJSON *ts = cJSON_GetObjectItem(inner, "ts");      // freshness (clock is SNTP-synced)
        if (cJSON_IsNumber(ts)) {
            long dt = (long)time(NULL) - (long)ts->valuedouble;
            if (dt < -window || dt > window) {
                ha_mqtt_log("cmd rejected: stale (dt=%lds win=%lds)", dt, window);
                cJSON_Delete(inner); cJSON_Delete(root); return;
            }
            // Monotonic (ts,seq) anti-replay: within the window, refuse anything not STRICTLY newer than
            // the last command we acted on — closes the replay gap the time-window alone leaves open.
            long mts = (long)ts->valuedouble;
            const cJSON *sq = cJSON_GetObjectItem(inner, "seq");
            int mseq = cJSON_IsNumber(sq) ? (int)sq->valuedouble : 0;
            replay_load();
            if (s_repl_ts >= 0 && (mts < s_repl_ts || (mts == s_repl_ts && mseq <= s_repl_seq))) {
                ha_mqtt_log("cmd rejected: replay (ts=%ld seq=%d <= seen ts=%ld seq=%d)",
                            mts, mseq, s_repl_ts, s_repl_seq);
                cJSON_Delete(inner); cJSON_Delete(root); return;
            }
            s_repl_ts = mts; s_repl_seq = mseq; replay_store(mts, mseq);
        }
        cmd = inner;
    } else {
        // Signature now REQUIRED for every op, including ota (the unsigned recovery exception is gone).
        ha_mqtt_log("cmd rejected: unsigned (signature required)");
        cJSON_Delete(root); return;
    }

    dispatch_cmd(cmd);
    if (inner) cJSON_Delete(inner);
    cJSON_Delete(root);
}

// A signed Phase-B coverage directive on home/edge/<node>/relay: verify the {p,s} HMAC (same path as
// commands), then hand the inner relay_assign to ha_relay (epoch-guarded + persisted). Retained, so a
// reconnecting node re-reads its current assignment on subscribe.
static void handle_relay(const char *data, int len) {
    cJSON *root = cJSON_ParseWithLength(data, len);
    if (!root) { ESP_LOGW(TAG, "bad relay json"); return; }
    const cJSON *p = cJSON_GetObjectItem(root, "p");
    const cJSON *s = cJSON_GetObjectItem(root, "s");
    if (cJSON_IsString(p) && cJSON_IsString(s) && cmd_sig_ok(p->valuestring, s->valuestring))
        ha_relay_apply(p->valuestring);
    else
        ha_mqtt_log("relay rejected: bad-sig/unsigned");
    cJSON_Delete(root);
}

// A signed reach-census trigger on home/edge/<node>/reach/req (ADR-0023). Sig-only (same HMAC path as
// relay directives): a trigger is idempotent + harmless (it only prompts a metadata report), so it
// deliberately does NOT share the cmd anti-replay high-water mark — that keeps a frequent census trigger
// from ever colliding with and dropping a real actuation command. Verify the {p,s} HMAC, then report.
static void handle_reach_req(const char *data, int len) {
    cJSON *root = cJSON_ParseWithLength(data, len);
    if (!root) { ESP_LOGW(TAG, "bad reach/req json"); return; }
    const cJSON *p = cJSON_GetObjectItem(root, "p");
    const cJSON *s = cJSON_GetObjectItem(root, "s");
    if (cJSON_IsString(p) && cJSON_IsString(s) && cmd_sig_ok(p->valuestring, s->valuestring))
        ha_reach_report();
    else
        ha_mqtt_log("reach/req rejected: bad-sig/unsigned");
    cJSON_Delete(root);
}

static void on_mqtt(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data) {
    esp_mqtt_event_handle_t e = event_data;
    switch ((esp_mqtt_event_id_t)event_id) {
        case MQTT_EVENT_CONNECTED:
            s_connected = true;
            ESP_LOGI(TAG, "connected");
            if (s_cfg.on_connected) s_cfg.on_connected(s_cfg.user);
            esp_mqtt_client_publish(s_client, s_status_topic, s_online_msg, 0, 1, true);
            if (s_hello_msg[0])   // ADR-0036: retained self-describing identity for PWA intake discovery
                esp_mqtt_client_publish(s_client, s_hello_topic, s_hello_msg, 0, 1, true);
            esp_mqtt_client_subscribe(s_client, s_cmd_topic, 1);
            esp_mqtt_client_subscribe(s_client, s_relay_topic, 1);   // Phase B coverage directives (retained)
            if (s_cfg.enable_reach)
                esp_mqtt_client_subscribe(s_client, s_reach_req_topic, 1);   // ADR-0023 census trigger
            break;
        case MQTT_EVENT_DISCONNECTED:
            s_connected = false;
            ESP_LOGW(TAG, "disconnected");
            if (s_cfg.on_disconnected) s_cfg.on_disconnected(s_cfg.user);
            break;
        case MQTT_EVENT_DATA:
            if (e->topic_len == (int)strlen(s_cmd_topic) && strncmp(e->topic, s_cmd_topic, e->topic_len) == 0)
                handle_cmd(e->data, e->data_len);
            else if (e->topic_len == (int)strlen(s_relay_topic) && strncmp(e->topic, s_relay_topic, e->topic_len) == 0)
                handle_relay(e->data, e->data_len);
            else if (s_cfg.enable_reach && e->topic_len == (int)strlen(s_reach_req_topic) &&
                     strncmp(e->topic, s_reach_req_topic, e->topic_len) == 0)
                handle_reach_req(e->data, e->data_len);
            break;
        default:
            break;
    }
}

// ha_gatt platform seams: relay -> the canonical history topic; diagnostics -> ha_mqtt_log. (The shared
// component was ported FROM this node's gatt_history.c, so these forward to the exact same sinks it used.)
static void edge_gatt_publish(const char *mac, const char *json, void *user) { (void)user; ha_mqtt_publish_history(mac, json); }
static void edge_gatt_log(const char *msg, void *user) { (void)user; ha_mqtt_log("%s", msg); }
// ha_gatt_exec reply seam: reply lines -> home/edge/<node>/<reqid>/reply (was ha_mqtt_publish_reply direct).
static void edge_exec_reply(const char *reqid, const char *json, void *user) { (void)user; ha_mqtt_publish_reply(reqid, json); }

// ha_ota platform seams: self-test = broker reachable; radio pause/resume = the single-radio BLE scanner;
// on_fail forwards to the board hook (e.g. the S3 operability LED) if one was installed.
static bool edge_ota_healthy(void *user) { (void)user; return ha_mqtt_is_connected(); }
static void edge_ota_radio_pause(void *user) { (void)user; ha_ble_scan_pause(); }
static void edge_ota_radio_resume(void *user) { (void)user; ha_ble_scan_resume(); }
static void edge_ota_on_fail(void *user) { if (s_cfg.ota_on_fail) s_cfg.ota_on_fail(s_cfg.user); }

// ADR-0036 intake: map the runtime chip model to the target string the server keys on.
static const char *chip_model_str(esp_chip_model_t m) {
    switch (m) {
        case CHIP_ESP32C6: return "esp32c6";
        case CHIP_ESP32S3: return "esp32s3";
        case CHIP_ESP32C3: return "esp32c3";
        case CHIP_ESP32:   return "esp32";
        default:           return "esp32?";
    }
}

// Build the retained self-describing identity doc for home/edge/<node>/hello — identity ONLY (never the
// secret). node/chip/mac/fw/slot let the server surface this unit as an intake candidate; abilities is the
// comma-list from the board (app_main) rendered as a JSON array. Called once from ha_mqtt_start.
static void build_hello(const esp_partition_t *run) {
    esp_chip_info_t ci; esp_chip_info(&ci);
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    char macbuf[18];
    snprintf(macbuf, sizeof(macbuf), "%02x:%02x:%02x:%02x:%02x:%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

    cJSON *root = cJSON_CreateObject();
    cJSON_AddNumberToObject(root, "schema", 1);
    cJSON_AddStringToObject(root, "node", s_node);
    cJSON_AddStringToObject(root, "chip", chip_model_str(ci.model));
    cJSON_AddStringToObject(root, "mac", macbuf);
    cJSON_AddStringToObject(root, "fw", s_cfg.fw_version ? s_cfg.fw_version : "?");
    cJSON_AddStringToObject(root, "slot", run ? run->label : "?");
    // enrolled = does this node hold a usable command secret yet? (Layer-0 self-provision flips this true
    // once claimed; today it just mirrors the compiled secret so the server can flag un-adopted hardware.)
    cJSON_AddBoolToObject(root, "enrolled", ha_mqtt_has_cmd_secret());
    cJSON *abil = cJSON_CreateArray();
    if (s_cfg.abilities && s_cfg.abilities[0]) {
        char tmp[128];
        snprintf(tmp, sizeof(tmp), "%s", s_cfg.abilities);
        for (char *save = NULL, *tok = strtok_r(tmp, ",", &save); tok; tok = strtok_r(NULL, ",", &save)) {
            while (*tok == ' ') tok++;                       // trim leading spaces
            if (*tok) cJSON_AddItemToArray(abil, cJSON_CreateString(tok));
        }
    }
    cJSON_AddItemToObject(root, "abilities", abil);

    char *s = cJSON_PrintUnformatted(root);
    if (s) { snprintf(s_hello_msg, sizeof(s_hello_msg), "%s", s); cJSON_free(s); }
    cJSON_Delete(root);
}

void ha_mqtt_start(const char *broker_uri, const char *node_id) {
    ha_gatt_init(&(ha_gatt_cfg_t){ .publish = edge_gatt_publish, .log = edge_gatt_log });
    ha_gatt_exec_init(&(ha_gatt_exec_cfg_t){ .publish_reply = edge_exec_reply, .log = edge_gatt_log });
    ha_ota_init(&(ha_ota_cfg_t){ .node_id = node_id, .ota_host = s_cfg.ota_host, .log = edge_gatt_log,
                                 .is_healthy = edge_ota_healthy, .on_fail = edge_ota_on_fail,
                                 .radio_pause = edge_ota_radio_pause, .radio_resume = edge_ota_radio_resume });
    snprintf(s_node, sizeof(s_node), "%s", node_id);
    snprintf(s_status_topic, sizeof(s_status_topic), "home/edge/%s/status", s_node);
    snprintf(s_cmd_topic, sizeof(s_cmd_topic), "home/edge/%s/cmd", s_node);
    snprintf(s_relay_topic, sizeof(s_relay_topic), "home/edge/%s/relay", s_node);
    snprintf(s_reach_req_topic, sizeof(s_reach_req_topic), "home/edge/%s/reach/req", s_node);
    snprintf(s_hello_topic, sizeof(s_hello_topic), "home/edge/%s/hello", s_node);
    const esp_partition_t *run = esp_ota_get_running_partition();
    snprintf(s_online_msg, sizeof(s_online_msg), "online %s %s", run ? run->label : "?",
             s_cfg.fw_version ? s_cfg.fw_version : "?");
    build_hello(run);   // ADR-0036: compose the retained identity doc once (published on each connect)

    esp_mqtt_client_config_t cfg = {
        .broker.address.uri = broker_uri,
        // Latent broker creds: NULL when empty (anonymous today); used after the auth cutover.
        .credentials.username = (s_cfg.mqtt_user && s_cfg.mqtt_user[0]) ? s_cfg.mqtt_user : NULL,
        .credentials.authentication.password = (s_cfg.mqtt_pass && s_cfg.mqtt_pass[0]) ? s_cfg.mqtt_pass : NULL,
        .session.last_will = { .topic = s_status_topic, .msg = "offline", .msg_len = 0, .qos = 1, .retain = true },
        .session.keepalive = 30,
        .network.reconnect_timeout_ms = 5000,
    };
    s_client = esp_mqtt_client_init(&cfg);
    esp_mqtt_client_register_event(s_client, ESP_EVENT_ANY_ID, on_mqtt, NULL);
    esp_mqtt_client_start(s_client);
}

bool ha_mqtt_is_connected(void) { return s_connected; }

// Generic relayed-BLE-device publish (ADR-0020): emit the canonical home/edge/<node>/<mac>/adv envelope
// with a caller-built device_type + metrics JSON. edge_mapper ingests it by MAC (device-type-agnostic),
// so any decoder (SwitchBot, Aranet, …) reuses this — the server needs no per-type change.
void ha_mqtt_publish_device(const char *mac_str, const char *device_type, const char *metrics_json, int rssi) {
    if (!s_connected) return;
    char mf[13]; macflat(mac_str, mf);
    char topic[80];
    snprintf(topic, sizeof(topic), "home/edge/%s/%s/adv", s_node, mf);
    char ts[24];
    if (!ha_sntp_iso_utc(ts, sizeof(ts))) ts[0] = '\0';
    char payload[320];
    int n = snprintf(payload, sizeof(payload),
        "{\"schema\":1,\"node\":\"%s\",\"mac\":\"%s\",\"device_type\":\"%s\","
        "\"ts\":\"%s\",\"transport\":\"ble-adv\",\"metrics\":%s,\"meta\":{\"rssi\":%d}}",
        s_node, mac_str, device_type, ts, metrics_json, rssi);
    if (n <= 0 || n >= (int)sizeof(payload)) return;
    esp_mqtt_client_publish(s_client, topic, payload, n, 1, false);
}

void ha_mqtt_publish_reading(const char *mac_str, const sb_reading_t *r, int rssi) {
    char metrics[96];
    if (r->battery_pct >= 0)
        snprintf(metrics, sizeof(metrics), "{\"temperature_c\":%.1f,\"humidity_pct\":%d,\"battery_pct\":%d}",
                 r->temperature_c, r->humidity_pct, r->battery_pct);
    else
        snprintf(metrics, sizeof(metrics), "{\"temperature_c\":%.1f,\"humidity_pct\":%d}",
                 r->temperature_c, r->humidity_pct);
    ha_mqtt_publish_device(mac_str, r->device_type, metrics, rssi);
}

void ha_mqtt_publish_history(const char *mac_str, const char *payload) {
    if (!s_connected) return;
    char mf[13]; macflat(mac_str, mf);
    char topic[80];
    snprintf(topic, sizeof(topic), "home/edge/%s/%s/history", s_node, mf);
    esp_mqtt_client_publish(s_client, topic, payload, 0, 1, false);
}

void ha_mqtt_publish_node_sensor(const char *key, const char *reg_key,
                                 const char *device_type, const char *metrics_json) {
    if (!s_connected) return;
    char topic[80];
    snprintf(topic, sizeof(topic), "home/edge/%s/%s/adv", s_node, key);
    char ts[24];
    if (!ha_sntp_iso_utc(ts, sizeof(ts))) ts[0] = '\0';
    char payload[320];
    int n = snprintf(payload, sizeof(payload),
        "{\"schema\":1,\"node\":\"%s\",\"mac\":\"%s\",\"device_type\":\"%s\","
        "\"ts\":\"%s\",\"transport\":\"i2c-local\",\"metrics\":%s,\"meta\":{}}",
        s_node, reg_key, device_type, ts, metrics_json);
    if (n <= 0 || n >= (int)sizeof(payload)) return;
    esp_mqtt_client_publish(s_client, topic, payload, n, 1, false);
}

void ha_mqtt_publish_reply(const char *reqid, const char *payload) {
    if (!s_connected) return;
    char topic[80];
    snprintf(topic, sizeof(topic), "home/edge/%s/%s/reply", s_node, reqid);
    esp_mqtt_client_publish(s_client, topic, payload, 0, 1, false);
}

void ha_mqtt_publish_reach(const char *reach_json) {
    if (!s_connected) return;
    char topic[64];
    snprintf(topic, sizeof(topic), "home/edge/%s/reach", s_node);
    char ts[24];
    if (!ha_sntp_iso_utc(ts, sizeof(ts))) ts[0] = '\0';
    char payload[1152];
    int n = snprintf(payload, sizeof(payload),
        "{\"schema\":1,\"node\":\"%s\",\"ts\":\"%s\",\"reach\":%s}", s_node, ts, reach_json);
    if (n <= 0 || n >= (int)sizeof(payload)) return;   // oversized → drop (next census reports fresh)
    esp_mqtt_client_publish(s_client, topic, payload, n, 1, false);
}

void ha_mqtt_log(const char *fmt, ...) {
    char msg[200];
    va_list ap; va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    ESP_LOGI("ha_edge", "%s", msg);          // also goes to serial when attached
    if (!s_connected) return;
    char topic[64];
    snprintf(topic, sizeof(topic), "home/edge/%s/log", s_node);
    esp_mqtt_client_publish(s_client, topic, msg, 0, 0, false);
}
