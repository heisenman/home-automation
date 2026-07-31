// Node config loader (ADR-0020) — see ha_config.h. Promoted from edge/esp32c6/main/ha_config.c; the only
// change vs the fork is the secrets seam: compile-time defaults arrive as a parameter (app_main reads the
// board-local secrets.h and passes them), so this shared component never includes secrets. NVS overlay +
// provisioning precedence are unchanged.
#include "ha_config.h"
#include <string.h>
#include <stdio.h>
#include "nvs.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_random.h"         // ADR-0036 Layer 0: HW RNG for the node-born secret (radio must be up)
#include "esp_mac.h"            // ADR-0036 Layer 0: base MAC as the secret's domain separator
#include "mbedtls/md.h"         // ADR-0036 Layer 0: HMAC-SHA256 construction
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ha_config";

// ADR-0036 Layer 0/3 provisioning state. The secret itself lives in the "ha" namespace beside the rest of
// the overlay (so a repoint backup/restore never touches it); the one-shot CLAIM flag lives here, apart
// from config, because it is a security latch rather than a setting — clearing config must not silently
// re-open the enroll window.
#define PROV_NS      "ha_prov"
#define PROV_CLAIMED "claimed"

// Repoint boot-count revert (DJ-19): a separate NVS namespace holds the pending flag, the trial-boot
// counter, and a backup of the pre-repoint effective config. After RP_MAX_TRIES failed trial boots the
// node reverts to the backup and reboots — so a bad repoint recovers over the air, never a physical trip.
#define RP_NS        "ha_repoint"
#define RP_MAX_TRIES 3

// The effective config as last loaded, cached so ha_config_repoint_apply can back up a known-good state
// (including compile defaults for keys that weren't NVS-overlaid) before it writes the new values.
static ha_config_t s_effective;
static bool s_have_effective;

// Overlay one string key from NVS namespace "ha" if present. Returns true if NVS supplied the value —
// the caller uses that for cmd_secret to tell a PROVISIONED secret (node-born or written by a flashing
// tool) from a compile-time one, which is what distinguishes adoptable hardware from a legacy enrolled
// node. Do NOT log the value of a secret key; the key name alone is fine.
static bool nvs_overlay(nvs_handle_t h, const char *key, char *dst, size_t dst_sz) {
    size_t len = dst_sz;
    if (nvs_get_str(h, key, dst, &len) == ESP_OK) {
        ESP_LOGI(TAG, "config[%s] from NVS", key);
        return true;
    }
    return false;
}

// Did cmd_secret come from NVS (node-born / provisioned) rather than the compiled defaults? Persisted
// implicitly: it is recomputed from NVS on every ha_config_load, so it survives reboots correctly. A
// compile-time secret means the node was enrolled at build time (ADR-0020) and is claimed by
// construction; an NVS secret means nobody necessarily knows it yet.
static bool s_secret_from_nvs;

void ha_config_load(ha_config_t *cfg, const ha_config_t *defaults) {
    // 1) compile-time defaults (board's secrets.h, passed by app_main)
    *cfg = *defaults;

    // 2) NVS overlay (production provisioning) — best-effort
    nvs_handle_t h;
    if (nvs_open("ha", NVS_READONLY, &h) == ESP_OK) {
        nvs_overlay(h, "wifi_ssid", cfg->wifi_ssid, sizeof(cfg->wifi_ssid));
        nvs_overlay(h, "wifi_psk", cfg->wifi_psk, sizeof(cfg->wifi_psk));
        nvs_overlay(h, "broker_uri", cfg->broker_uri, sizeof(cfg->broker_uri));
        nvs_overlay(h, "node_id", cfg->node_id, sizeof(cfg->node_id));
        nvs_overlay(h, "ntp_server", cfg->ntp_server, sizeof(cfg->ntp_server));
        nvs_overlay(h, "ota_host", cfg->ota_host, sizeof(cfg->ota_host));
        // ADR-0036 Layer 0. Same overlay call, but note the PRECEDENCE here is the same as every other
        // key (NVS wins over the compile default) — which for the secret is the security-critical
        // direction: a node-born secret must never be shadowed by a stale build-time one.
        s_secret_from_nvs = nvs_overlay(h, "cmd_secret", cfg->cmd_secret, sizeof(cfg->cmd_secret));
        nvs_close(h);
    }
    s_effective = *cfg;          // cache for repoint backup
    s_have_effective = true;
    // NB: cmd_secret is deliberately absent from this line — we log its PRESENCE, never its value.
    ESP_LOGI(TAG, "node=%s broker=%s ntp=%s ssid=%s ota_host=%s secret=%s",
             cfg->node_id, cfg->broker_uri, cfg->ntp_server, cfg->wifi_ssid, cfg->ota_host,
             cfg->cmd_secret[0] ? "yes" : "no");
}

// --- ADR-0036 Layer 0: node-born command secret -----------------------------------------------------

bool ha_config_is_claimed(void) {
    nvs_handle_t h;
    if (nvs_open(PROV_NS, NVS_READONLY, &h) == ESP_OK) {   // namespace absent on a never-claimed node
        uint8_t v = 0;
        esp_err_t e = nvs_get_u8(h, PROV_CLAIMED, &v);
        nvs_close(h);
        if (e == ESP_OK && v == 1) return true;
    }
    // Legacy path: a COMPILE-TIME secret means the node was enrolled at build time (ADR-0020) and is
    // claimed by construction — it must never show up in the PWA as adoptable standby hardware. A secret
    // that came from NVS proves nothing: it may be node-born and still unknown to any dictator.
    return s_have_effective && s_effective.cmd_secret[0] && !s_secret_from_nvs;
}

bool ha_config_mark_claimed(void) {
    nvs_handle_t h;
    if (nvs_open(PROV_NS, NVS_READWRITE, &h) != ESP_OK) return false;
    esp_err_t e = nvs_set_u8(h, PROV_CLAIMED, 1);
    if (e == ESP_OK) e = nvs_commit(h);
    nvs_close(h);
    if (e != ESP_OK) { ESP_LOGE(TAG, "claim: NVS commit failed (%s)", esp_err_to_name(e)); return false; }
    ESP_LOGW(TAG, "claimed by dictator — enroll window CLOSED (wipe NVS to re-adopt)");
    return true;
}

bool ha_config_ensure_node_secret(ha_config_t *cfg) {
    if (!cfg) return false;

    // 1) / 2) Already have one (NVS-overlaid by ha_config_load, or compiled in) — never regenerate.
    // Regenerating would orphan the node: the dictator's LUT still holds the old secret, so every signed
    // command (including the OTA that could fix it) would fail verification.
    if (cfg->cmd_secret[0]) return true;

    // 3) Generate. esp_random() is a TRUE hardware RNG only once the radio is up — the caller guarantees
    // that by calling us after network bring-up. 32 random bytes are the entropy; the base MAC is a
    // domain separator so two units can never collide even in the pathological case of a shared RNG
    // stream. Deriving from the MAC ALONE would be predictable (a MAC is public), hence HMAC(random, mac)
    // and not HMAC(mac, anything).
    uint8_t key[32];
    esp_fill_random(key, sizeof(key));
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);

    unsigned char out[32];
    const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    if (!info || mbedtls_md_hmac(info, key, sizeof(key), mac, sizeof(mac), out) != 0) {
        ESP_LOGE(TAG, "secret: HMAC failed — node stays unprovisioned");
        return false;
    }
    char hex[65];
    for (int i = 0; i < 32; i++) snprintf(hex + i * 2, 3, "%02x", out[i]);
    hex[64] = '\0';

    // Persist BEFORE adopting it in-process: if the commit fails we must not run with a secret the node
    // would forget on reboot (the dictator would enroll a secret that dies at the next power cycle).
    nvs_handle_t h;
    esp_err_t e = nvs_open("ha", NVS_READWRITE, &h);
    if (e == ESP_OK) {
        e = nvs_set_str(h, "cmd_secret", hex);
        if (e == ESP_OK) e = nvs_commit(h);
        nvs_close(h);
    }
    if (e != ESP_OK) {
        ESP_LOGE(TAG, "secret: NVS persist failed (%s) — node stays unprovisioned", esp_err_to_name(e));
        return false;
    }

    snprintf(cfg->cmd_secret, sizeof(cfg->cmd_secret), "%s", hex);
    s_effective = *cfg;                 // keep the repoint backup in step with reality
    s_have_effective = true;
    s_secret_from_nvs = true;           // node-born => NVS-sourced => UNCLAIMED until a dictator claims it
    ESP_LOGW(TAG, "node-born command secret generated + persisted (unclaimed; awaiting dictator intake)");
    return true;
}

bool ha_config_repoint_apply(const char *ssid, const char *psk, const char *broker,
                             const char *ntp, const char *ota_host) {
    if (!broker || !broker[0]) {
        ESP_LOGE(TAG, "repoint: missing broker — refused");
        return false;
    }
    // ssid/psk are OPTIONAL: a WIRED node (S3-ETH) repoints broker-only and keeps its existing Wi-Fi
    // fallback creds; a Wi-Fi node passes ssid+psk to also switch SSID. NULL/"" = leave that key unchanged.
    // 1) Arm the revert: back up the current effective config + set pending, in one commit. If we have no
    //    cached effective config (shouldn't happen — load runs first), back up empties so a revert clears
    //    the overlay to compile defaults rather than restoring garbage.
    const ha_config_t *e = s_have_effective ? &s_effective : NULL;
    nvs_handle_t r;
    if (nvs_open(RP_NS, NVS_READWRITE, &r) != ESP_OK) { ESP_LOGE(TAG, "repoint: nvs open %s failed", RP_NS); return false; }
    nvs_set_str(r, "bk_ssid",   e ? e->wifi_ssid  : "");
    nvs_set_str(r, "bk_psk",    e ? e->wifi_psk   : "");
    nvs_set_str(r, "bk_broker", e ? e->broker_uri : "");
    nvs_set_str(r, "bk_ntp",    e ? e->ntp_server : "");
    nvs_set_str(r, "bk_host",   e ? e->ota_host   : "");
    nvs_set_u8(r, "pending", 1);
    nvs_set_u8(r, "tries", 0);
    if (nvs_commit(r) != ESP_OK) { nvs_close(r); ESP_LOGE(TAG, "repoint: arm commit failed"); return false; }
    nvs_close(r);

    // 2) Write the new network params into the "ha" overlay (node_id left untouched).
    nvs_handle_t h;
    esp_err_t err = nvs_open("ha", NVS_READWRITE, &h);
    if (err == ESP_OK) {
        nvs_set_str(h, "broker_uri", broker);
        if (ssid && ssid[0])         nvs_set_str(h, "wifi_ssid", ssid);   // optional (wired node omits)
        if (psk && psk[0])           nvs_set_str(h, "wifi_psk", psk);     // optional
        if (ntp && ntp[0])           nvs_set_str(h, "ntp_server", ntp);
        if (ota_host && ota_host[0]) nvs_set_str(h, "ota_host", ota_host);
        err = nvs_commit(h);
        nvs_close(h);
    }
    if (err != ESP_OK) {
        // New config didn't land — disarm so we don't count pointless boots; node stays on old config.
        ESP_LOGE(TAG, "repoint: overlay write failed (%s) — disarming, not moving", esp_err_to_name(err));
        if (nvs_open(RP_NS, NVS_READWRITE, &r) == ESP_OK) { nvs_set_u8(r, "pending", 0); nvs_commit(r); nvs_close(r); }
        return false;
    }

    // 3) Reboot into the new config, armed for revert.
    // NB: ssid is NULL on a broker-only repoint (wired S3-ETH, or a Wi-Fi node moving broker only) — %s on
    // NULL is a Load-access-fault panic on ESP32 newlib-nano, so guard it. (Caught on the D1001 bench,
    // 2026-07-09: a broker-only repoint panicked here; it self-healed only because the overlay was already
    // committed above before the crash.)
    ESP_LOGW(TAG, "repoint ARMED: ssid=%s broker=%s — rebooting (revert after %d failed boots)",
             ssid ? ssid : "(unchanged)", broker, RP_MAX_TRIES);
    vTaskDelay(pdMS_TO_TICKS(500));   // let the log flush + the MQTT ack settle
    esp_restart();
    return true;                      // unreachable
}

void ha_config_repoint_boot_check(void) {
    nvs_handle_t r;
    if (nvs_open(RP_NS, NVS_READONLY, &r) != ESP_OK) return;   // namespace absent -> nothing pending
    uint8_t pending = 0, tries = 0;
    esp_err_t ep = nvs_get_u8(r, "pending", &pending);
    nvs_get_u8(r, "tries", &tries);
    char bk_ssid[33] = "", bk_psk[64] = "", bk_broker[96] = "", bk_ntp[64] = "", bk_host[64] = "";
    size_t n;
    n = sizeof(bk_ssid);   nvs_get_str(r, "bk_ssid",   bk_ssid,   &n);
    n = sizeof(bk_psk);    nvs_get_str(r, "bk_psk",    bk_psk,    &n);
    n = sizeof(bk_broker); nvs_get_str(r, "bk_broker", bk_broker, &n);
    n = sizeof(bk_ntp);    nvs_get_str(r, "bk_ntp",    bk_ntp,    &n);
    n = sizeof(bk_host);   nvs_get_str(r, "bk_host",   bk_host,   &n);
    nvs_close(r);
    if (ep != ESP_OK || !pending) return;

    if (tries >= RP_MAX_TRIES) {
        // Revert: restore the backed-up config into "ha", clear pending, reboot onto the last-good net.
        nvs_handle_t h;
        if (nvs_open("ha", NVS_READWRITE, &h) == ESP_OK) {
            nvs_set_str(h, "wifi_ssid", bk_ssid);
            nvs_set_str(h, "wifi_psk", bk_psk);
            nvs_set_str(h, "broker_uri", bk_broker);
            nvs_set_str(h, "ntp_server", bk_ntp);
            nvs_set_str(h, "ota_host", bk_host);
            nvs_commit(h);
            nvs_close(h);
        }
        if (nvs_open(RP_NS, NVS_READWRITE, &r) == ESP_OK) {
            nvs_set_u8(r, "pending", 0); nvs_set_u8(r, "tries", 0); nvs_commit(r); nvs_close(r);
        }
        ESP_LOGE(TAG, "repoint REVERTED after %d failed boots -> restoring ssid=%s broker=%s; rebooting",
                 tries, bk_ssid, bk_broker);
        vTaskDelay(pdMS_TO_TICKS(500));
        esp_restart();
    }
    // Count this trial boot and continue (the late confirm clears pending if the broker comes up).
    if (nvs_open(RP_NS, NVS_READWRITE, &r) == ESP_OK) {
        nvs_set_u8(r, "tries", (uint8_t)(tries + 1)); nvs_commit(r); nvs_close(r);
    }
    ESP_LOGW(TAG, "repoint trial boot %d/%d (awaiting broker confirm)", tries + 1, RP_MAX_TRIES);
}

void ha_config_repoint_confirm(bool (*is_healthy)(void *user), void *user, int timeout_ms) {
    nvs_handle_t r;
    if (nvs_open(RP_NS, NVS_READONLY, &r) != ESP_OK) return;
    uint8_t pending = 0;
    esp_err_t ep = nvs_get_u8(r, "pending", &pending);
    nvs_close(r);
    if (ep != ESP_OK || !pending) return;

    int waited = 0;
    while (waited < timeout_ms) {
        if (is_healthy && is_healthy(user)) {
            if (nvs_open(RP_NS, NVS_READWRITE, &r) == ESP_OK) {
                nvs_set_u8(r, "pending", 0); nvs_set_u8(r, "tries", 0); nvs_commit(r); nvs_close(r);
            }
            ESP_LOGI(TAG, "repoint CONFIRMED (broker reachable) — committed");
            return;
        }
        vTaskDelay(pdMS_TO_TICKS(500));
        waited += 500;
    }
    ESP_LOGE(TAG, "repoint NOT confirmed in %dms — rebooting to retry/revert", timeout_ms);
    esp_restart();
}
