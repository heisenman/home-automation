// ha_power_policy — runtime (ADR-0024). Drives the injected actuators; ESP/FreeRTOS side.
// The decisions live in ha_power_policy.c (pure, host-tested); this file is only the loops +
// actuation glue, so it is intentionally thin.
#include "ha_power_policy.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"

static const char *TAG = "ha_pwr_policy";

// --- boot gate ---------------------------------------------------------------------------

typedef struct {
    ha_power_policy_cfg_t cfg;
    ha_power_policy_io_t  io;
    void (*on_safe)(void *ctx);
} gate_ctx_t;

// Runs only when the cell booted below the cold-start floor: blink the LED and poll until the
// cell recovers, then release the display and exit. WiFi/MQTT are already up (app_main was not
// blocked), so the device stays reachable the whole time it sits dark.
static void gate_task(void *pv)
{
    gate_ctx_t *g = (gate_ctx_t *)pv;
    ESP_LOGW(TAG, "boot gate: cell below cold-start floor (%d mV) — holding display dark, blinking LED",
             g->cfg.boot_gate_mv);
    bool led = false;
    for (;;) {
        int mv = g->io.read_mv(g->io.ctx);
        if (ha_power_policy_boot_ok(&g->cfg, mv, /*was_dark=*/true)) {
            if (g->io.led) g->io.led(g->io.ctx, false);
            ESP_LOGW(TAG, "boot gate: cell recovered to %d mV (>= %d) — releasing display",
                     mv, g->cfg.boot_release_mv);
            if (g->on_safe) g->on_safe(g->io.ctx);
            break;
        }
        led = !led;
        if (g->io.led) g->io.led(g->io.ctx, led);   // ~1 Hz "CHARGE THE DAMN BATTERY" blink
        vTaskDelay(pdMS_TO_TICKS(500));
    }
    vPortFree(g);
    vTaskDelete(NULL);
}

void ha_power_policy_boot_gate(const ha_power_policy_cfg_t *cfg,
                               const ha_power_policy_io_t *io,
                               void (*on_safe)(void *ctx))
{
    int mv = io->read_mv(io->ctx);
    if (ha_power_policy_boot_ok(cfg, mv, /*was_dark=*/false)) {
        if (on_safe) on_safe(io->ctx);              // fast path: already safe, release immediately
        return;
    }
    gate_ctx_t *g = pvPortMalloc(sizeof(*g));
    if (!g) {                                       // OOM: fail SAFE-toward-usable — release the display
        ESP_LOGE(TAG, "boot gate: OOM — releasing display without gating");
        if (on_safe) on_safe(io->ctx);
        return;
    }
    g->cfg = *cfg; g->io = *io; g->on_safe = on_safe;
    xTaskCreate(gate_task, "pwr_gate", 3072, g, 4, NULL);
}

// --- steady-state monitor ----------------------------------------------------------------

typedef struct {
    ha_power_policy_cfg_t cfg;
    ha_power_policy_io_t  io;
    int period_ms;
} mon_ctx_t;

static void monitor_task(void *pv)
{
    mon_ctx_t *m = (mon_ctx_t *)pv;
    ha_pp_state_t st = {0};
    for (;;) {
        int mv = m->io.read_mv(m->io.ctx);
        switch (ha_power_policy_eval(&m->cfg, &st, mv)) {
        case HA_PP_SHUTDOWN:
            ESP_LOGE(TAG, "cell at run floor (%d mV <= %d) — HARD POWER-OFF", mv, m->cfg.shutdown_mv);
            if (m->io.warn) m->io.warn(m->io.ctx, true);   // last banner before the rail drops
            if (m->io.power_off) m->io.power_off(m->io.ctx);
            // On battery power_off never returns; on USB it does — keep sampling so we trip
            // again the instant USB is pulled, rather than sitting dark-but-alive below floor.
            break;
        case HA_PP_WARN_ON:
            ESP_LOGW(TAG, "cell low (%d mV <= %d) — warning", mv, m->cfg.warn_mv);
            if (m->io.warn) m->io.warn(m->io.ctx, true);
            break;
        case HA_PP_WARN_OFF:
            ESP_LOGW(TAG, "cell recovered (%d mV >= %d) — clearing warning", mv, m->cfg.warn_clear_mv);
            if (m->io.warn) m->io.warn(m->io.ctx, false);
            break;
        case HA_PP_NONE:
        default:
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(m->period_ms));
    }
}

void ha_power_policy_monitor_start(const ha_power_policy_cfg_t *cfg,
                                   const ha_power_policy_io_t *io, int period_ms)
{
    mon_ctx_t *m = pvPortMalloc(sizeof(*m));
    if (!m) { ESP_LOGE(TAG, "monitor: OOM — NOT started"); return; }
    m->cfg = *cfg; m->io = *io; m->period_ms = period_ms > 0 ? period_ms : 5000;
    xTaskCreate(monitor_task, "pwr_mon", 3072, m, 4, NULL);
}
