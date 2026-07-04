// Battery gauge + thermal-gated charging — see ha_battery.h.
#include "ha_battery.h"

#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "driver/gpio.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "ha_imu.h"          // board temp now comes from the shared IMU component (see ha_imu/README: composition)

static const char *TAG = "ha_batt";

#define ADC_SAMPLES 16
#define AVG_WIN      8
#define FULL_DETECT_MV 3700   // sanity floor for the charge-terminated→100% anchor (reject a low-cell plug-in transient)

// reTerminal D1001 21-point voltage LUT (3262 mV = 0% … 4047 mV = 100%, 5%/step).
static const int D1001_LUT[21] = { 3262,3390,3467,3554,3619,3659,3686,3710,3731,3752,
                                   3774,3797,3827,3855,3880,3901,3915,3934,3958,3978,4047 };

ha_battery_cfg_t ha_battery_d1001_cfg(esp_io_expander_handle_t io_expander,
                                      i2c_master_bus_handle_t i2c_bus)
{
    ha_battery_cfg_t c = {
        .adc_unit = 0 /*ADC_UNIT_1*/, .adc_ch_batt = 2 /*CH2*/, .adc_ch_usb = 1 /*CH1*/,
        .adc_atten = 3 /*ADC_ATTEN_DB_12*/, .divider_mul = 2,
        .io_expander = io_expander, .exp_read_en_mask = (1u << 6), .exp_charge_en_mask = (1u << 10),
        .gpio_charge = 15, .gpio_vsys_pg = 4,
        .i2c_bus = i2c_bus, .imu_addr = 0x6A,
        .lut = D1001_LUT, .lut_n = 21,
        .usb_present_mv = 4000, .volt_high_mv = 4150, .volt_recharge_mv = 3800,  // factory hysteresis band
        .temp_min_dc = 20, .temp_max_dc = 430,
    };
    return c;
}

static ha_battery_cfg_t s_cfg;
static bool s_have_cfg;
static ha_batt_profile_t s_profile;   // state-normalized V→SoC profile (ADR-0024 §5)
static bool s_have_profile;
static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_cali_batt, s_cali_usb;
static bool s_init;
static bool s_read_en;
static int  s_smooth_mv;
static int  s_win[AVG_WIN], s_win_n, s_win_i;
static int      s_trend_ref_mv;    // ~60 s-ago smoothed mV, for the "gaining" trend
static int64_t  s_trend_ref_us;
static bool     s_gaining;         // cell voltage rising (actively charging), see ha_battery_sample
static volatile bool s_charge_en;
static volatile int  s_charge_mode = 0;   // HA_CHG_AUTO
static volatile int  s_pulse_ms = 0;      // one-shot /CE reset-pulse request (ms)
static SemaphoreHandle_t s_mtx;

// (LSM6DS3TR-C IMU register access lives in the ha_imu shared component now)

static void read_enable(void)   // assert the ADC sense divider once the expander is up
{
    if (s_read_en || !s_cfg.io_expander || !s_cfg.exp_read_en_mask) return;
    esp_io_expander_set_dir(s_cfg.io_expander, s_cfg.exp_read_en_mask, IO_EXPANDER_OUTPUT);
    esp_io_expander_set_level(s_cfg.io_expander, s_cfg.exp_read_en_mask, 1);   // active-high
    s_read_en = true;
}

static void batt_adc_init(void)
{
    if (s_init) return;
    // Serialize: this is reached from ha_battery_init, ha_battery_sample (the periodic sample
    // task) AND ha_battery_charge_start on the dual-core P4. Without the lock two callers pass
    // the s_init gate together, both call adc_oneshot_new_unit(ADC_UNIT_1), the loser logs
    // "adc1 already in use" — and, worse, into a LOCAL handle so it can never clobber the good
    // one. Only publish s_adc/s_init on full success.
    if (s_mtx) xSemaphoreTake(s_mtx, portMAX_DELAY);
    if (s_init) { if (s_mtx) xSemaphoreGive(s_mtx); return; }   // re-check under the lock

    adc_oneshot_unit_handle_t adc = NULL;
    adc_oneshot_unit_init_cfg_t uc = { .unit_id = s_cfg.adc_unit, .ulp_mode = ADC_ULP_MODE_DISABLE };
    if (adc_oneshot_new_unit(&uc, &adc) != ESP_OK) {
        ESP_LOGW(TAG, "adc unit fail");
        if (s_mtx) xSemaphoreGive(s_mtx);
        return;
    }
    adc_oneshot_chan_cfg_t cc = { .atten = s_cfg.adc_atten, .bitwidth = ADC_BITWIDTH_12 };
    adc_oneshot_config_channel(adc, s_cfg.adc_ch_batt, &cc);
    if (s_cfg.adc_ch_usb >= 0) adc_oneshot_config_channel(adc, s_cfg.adc_ch_usb, &cc);
#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    adc_cali_curve_fitting_config_t fc = { .unit_id = s_cfg.adc_unit, .atten = s_cfg.adc_atten,
                                           .bitwidth = ADC_BITWIDTH_12 };
    fc.chan = s_cfg.adc_ch_batt; adc_cali_create_scheme_curve_fitting(&fc, &s_cali_batt);
    if (s_cfg.adc_ch_usb >= 0) { fc.chan = s_cfg.adc_ch_usb; adc_cali_create_scheme_curve_fitting(&fc, &s_cali_usb); }
#endif
    uint64_t mask = 0;
    if (s_cfg.gpio_charge  >= 0) mask |= 1ULL << s_cfg.gpio_charge;
    if (s_cfg.gpio_vsys_pg >= 0) mask |= 1ULL << s_cfg.gpio_vsys_pg;
    if (mask) {
        gpio_config_t g = { .pin_bit_mask = mask, .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_ENABLE };
        gpio_config(&g);
    }
    s_adc = adc;         // publish only on full success — losers never see a half-init handle
    s_init = true;
    if (s_mtx) xSemaphoreGive(s_mtx);
}

static int lut_pct(int mv)
{
    const int *L = s_cfg.lut; int n = s_cfg.lut_n;
    if (mv < L[0]) return 0;
    for (int i = 1; i < n; i++)
        if (mv < L[i]) return (i - 1) * 5 + 5 * (mv - L[i-1]) / (L[i] - L[i-1]);
    return 100;
}

// State-normalized SoC (ADR-0024). Normalizing out the display/USB/charging offsets means a state
// CHANGE (screen off, cable in) does not jump the reading — the offsets cancel. Two anchors the
// voltage curve can't give trustworthily are handled explicitly:
//   • 100% = the charger's own done signal: on wall, charge allowed (charge_en), STAT not-charging,
//     cell high ⇒ the BQ terminated ⇒ full, by the hardware's definition (Hugh). Voltage alone
//     mis-reads a full-on-charger cell (~95% base frame under the constant-offset model).
// Normalized SoC from a pre-normalized reading + state. `smooth_mv` (raw base-frame) still gates the
// hardware-100% anchor; `vnorm` drives the curve.
static int soc_from(int smooth_mv, int vnorm, bool on_wall, bool charging)
{
    if (on_wall && s_charge_en && !charging && smooth_mv >= FULL_DETECT_MV)
        return 100;
    if (s_have_profile) return ha_batt_profile_soc(&s_profile, vnorm);
    return lut_pct(smooth_mv);   // legacy raw-voltage LUT fallback
}

static int adc_read_mv(int ch, adc_cali_handle_t cali, int *raw_out)
{
    int raw[ADC_SAMPLES];
    for (int i = 0; i < ADC_SAMPLES; i++) { raw[i] = 0; adc_oneshot_read(s_adc, ch, &raw[i]); }
    for (int i = 0; i < ADC_SAMPLES - 1; i++)
        for (int j = i + 1; j < ADC_SAMPLES; j++)
            if (raw[i] > raw[j]) { int t = raw[i]; raw[i] = raw[j]; raw[j] = t; }
    long sum = 0;
    for (int i = 1; i < ADC_SAMPLES - 1; i++) sum += raw[i];
    int rawavg = sum / (ADC_SAMPLES - 2), mv = rawavg;
#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    if (cali) adc_cali_raw_to_voltage(cali, rawavg, &mv);
#endif
    if (raw_out) *raw_out = rawavg;
    return mv;
}

// Board temp is now read through the ha_imu shared component (ha_imu_temp_dc). ha_battery no longer
// owns the IMU device handle or config — ha_imu owns the one physical chip; both compose on it (both
// call the idempotent ha_imu_init). See ha_imu/README.md "Composition".

esp_err_t ha_battery_init(const ha_battery_cfg_t *cfg)
{
    if (!cfg) return ESP_ERR_INVALID_ARG;
    s_cfg = *cfg;
    ha_imu_init(&(ha_imu_cfg_t){ .bus = s_cfg.i2c_bus, .addr = s_cfg.imu_addr });  // shared IMU (idempotent)
    if (!s_cfg.lut) { s_cfg.lut = D1001_LUT; s_cfg.lut_n = 21; }
    s_profile = cfg->profile ? *cfg->profile : ha_batt_profile_d1001_default();
    s_have_profile = true;
    s_have_cfg = true;
    if (!s_mtx) s_mtx = xSemaphoreCreateMutex();
    batt_adc_init();
    return s_adc ? ESP_OK : ESP_FAIL;
}

esp_err_t ha_battery_set_profile(const ha_batt_profile_t *p)
{
    if (!ha_batt_profile_valid(p)) return ESP_ERR_INVALID_ARG;   // never accept a torn/corrupt curve
    if (s_mtx) xSemaphoreTake(s_mtx, portMAX_DELAY);
    s_profile = *p;                 // whole-struct copy: a concurrent sample sees old-or-new, never torn
    s_have_profile = true;
    if (s_mtx) xSemaphoreGive(s_mtx);
    return ESP_OK;
}

bool ha_battery_get_profile(ha_batt_profile_t *out)
{
    if (!out || !s_have_profile) return false;
    if (s_mtx) xSemaphoreTake(s_mtx, portMAX_DELAY);
    *out = s_profile;
    if (s_mtx) xSemaphoreGive(s_mtx);
    return true;
}

esp_err_t ha_battery_sample(ha_batt_sample_t *out)
{
    if (!s_have_cfg) return ESP_FAIL;
    batt_adc_init();
    if (!s_adc) return ESP_FAIL;
    if (s_mtx) xSemaphoreTake(s_mtx, portMAX_DELAY);
    read_enable();

    int raw2 = 0, raw1 = 0;
    int cali_mv = adc_read_mv(s_cfg.adc_ch_batt, s_cali_batt, &raw2);
    int usb_mv  = (s_cfg.adc_ch_usb >= 0) ? adc_read_mv(s_cfg.adc_ch_usb, s_cali_usb, &raw1) * s_cfg.divider_mul : 0;
    int batt_mv = cali_mv * s_cfg.divider_mul;
    if (batt_mv <= 0) { if (s_mtx) xSemaphoreGive(s_mtx); return ESP_FAIL; }

    s_win[s_win_i] = batt_mv;
    s_win_i = (s_win_i + 1) % AVG_WIN;
    if (s_win_n < AVG_WIN) s_win_n++;
    long avgsum = 0; for (int i = 0; i < s_win_n; i++) avgsum += s_win[i];
    int avg = (int)(avgsum / s_win_n);

    bool on_charger = (usb_mv > s_cfg.usb_present_mv);
    // Symmetric EMA (~4-sample time constant) — tracks both directions. Replaces the old
    // ratchet-down-only smoother that permanently latched SoC onto a transient load sag
    // (ADR-0024 §1: "no one-way smoothing"). State offsets, not the smoother, handle load steps.
    if (s_smooth_mv == 0) s_smooth_mv = avg;
    else                  s_smooth_mv += (avg - s_smooth_mv) / 4;

    // "gaining" = the cell voltage is actually RISING over ~60 s. This is the honest
    // "actively charging" signal: on a current-limited port the charger STAT pin can assert
    // "charging" while system load holds net cell current ≈ 0 (voltage flat). Sampled off the
    // smoothed mV against a ~60 s reference; only meaningful while on the charger.
    int64_t now_us = esp_timer_get_time();
    if (s_trend_ref_us == 0) { s_trend_ref_mv = s_smooth_mv; s_trend_ref_us = now_us; }
    else if (now_us - s_trend_ref_us >= 60000000LL) {   // evaluate the trend each ~60 s
        s_gaining = on_charger && (s_smooth_mv - s_trend_ref_mv >= 6);   // rose ≥6 mV ⇒ gaining
        s_trend_ref_mv = s_smooth_mv;
        s_trend_ref_us = now_us;
    }

    int tdc = 0; bool ht = (ha_imu_temp_dc(&tdc) == ESP_OK);
    if (out) {
        out->raw_ch2 = raw2;
        out->cali_mv = cali_mv;
        out->batt_mv = batt_mv;
        out->batt_mv_smoothed = s_smooth_mv;
        out->usb_mv = usb_mv;
        out->vsys_pg = (s_cfg.gpio_vsys_pg >= 0) ? gpio_get_level(s_cfg.gpio_vsys_pg) : -1;
        out->charging = (s_cfg.gpio_charge >= 0) && (gpio_get_level(s_cfg.gpio_charge) == 0);
        out->on_wall = on_charger;
        out->gaining = s_gaining;
        bool display_on = s_cfg.display_on_fn ? s_cfg.display_on_fn() : true;
        int vnorm = s_have_profile
            ? ha_batt_profile_normalize(&s_profile, s_smooth_mv, display_on, on_charger, out->charging)
            : s_smooth_mv;
        out->batt_mv_norm = vnorm;
        out->soc = soc_from(s_smooth_mv, vnorm, on_charger, out->charging);
        out->temp_dc = ht ? tdc : -9999;
        out->have_temp = ht;
        out->charge_en = s_charge_en;
    }
    if (s_mtx) xSemaphoreGive(s_mtx);
    return ESP_OK;
}

esp_err_t ha_battery_read(int *soc_pct, float *volts, bool *charging)
{
    ha_batt_sample_t s;
    if (ha_battery_sample(&s) != ESP_OK) return ESP_FAIL;
    if (soc_pct)  *soc_pct  = s.soc;
    if (volts)    *volts    = s.batt_mv_smoothed / 1000.0f;
    if (charging) *charging = s.charging;
    return ESP_OK;
}

static void charge_set(bool en)
{
    if (!s_cfg.io_expander || !s_cfg.exp_charge_en_mask) return;
    esp_io_expander_set_dir(s_cfg.io_expander, s_cfg.exp_charge_en_mask, IO_EXPANDER_OUTPUT);
    esp_io_expander_set_level(s_cfg.io_expander, s_cfg.exp_charge_en_mask, en ? 0 : 1);   // active-low
}

// Charge manager — MIRRORS the Seeed factory BSP (esp32_p4_re_terminal_d1001.c
// `bsp_battery_charge_task`), which is the proven-correct behaviour for this hardware:
//   • ENABLED by default (matches the CHG_ENBn 100k pull-down hardware default);
//   • simple voltage HYSTERESIS only (stop >volt_high, resume <volt_recharge);
//   • NO firmware thermal gate — the BQ25616's TS/NTC pin does thermal protection in HARDWARE
//     (the factory compiles its firmware thermal-protect OUT for exactly this reason);
//   • NO "restart watchdog" pulsing.
// History: our previous version disabled at startup, thermal-gated (fail-closed on a bad IMU read),
// and PULSED CHARGE_EN off→on every 15 s whenever STAT read "not charging". That was self-defeating:
// each pulse restarted the BQ's charge cycle so it never established → STAT never read charging →
// endless pulsing → cell stuck at ~equilibrium. Falsified by production reality (every shipped D1001
// charges) + confirmed by reading the factory BSP. See docs/hardware/reterminal-d1001.md.
static void charge_task(void *pv)
{
    s_charge_en = true;
    for (;;) {
        // one-shot commanded /CE-high reset pulse (clears a latched BQ), then resume the mode
        int pulse = s_pulse_ms;
        if (pulse > 0) {
            s_pulse_ms = 0;
            charge_set(false); vTaskDelay(pdMS_TO_TICKS(pulse)); charge_set(true);
            ESP_LOGW(TAG, "/CE reset pulse %dms", pulse);
        }
        ha_batt_sample_t s = {0};
        if (ha_battery_sample(&s) == ESP_OK) {
            switch (s_charge_mode) {
            case HA_CHG_HOLD:                       // hands off — manual cmd/exp control
                break;
            case HA_CHG_ON:  s_charge_en = true;  charge_set(true);  break;
            case HA_CHG_OFF: s_charge_en = false; charge_set(false); break;
            case HA_CHG_AUTO: default: {
                bool prev = s_charge_en;
                if (s.batt_mv > s_cfg.volt_high_mv)          s_charge_en = false;  // full → stop
                else if (s.batt_mv < s_cfg.volt_recharge_mv) s_charge_en = true;   // drained → resume
                if (s_charge_en != prev)
                    ESP_LOGW(TAG, "charge %s (batt=%dmV)", s_charge_en ? "ON" : "OFF", s.batt_mv);
                charge_set(s_charge_en);   // re-assert (heals a display-init stomp of the shared pin)
                break;
            }
            }
        }
        vTaskDelay(pdMS_TO_TICKS(2000));
    }
}

void ha_battery_charge_mode(int mode) { if (mode >= 0 && mode <= 3) s_charge_mode = mode; }
int  ha_battery_charge_mode_get(void) { return s_charge_mode; }
void ha_battery_charge_reset_pulse(int ms) { if (ms < 20) ms = 20; if (ms > 5000) ms = 5000; s_pulse_ms = ms; }

void ha_battery_charge_start(void)
{
    static bool started;
    if (started || !s_have_cfg) return;
    started = true;
    if (!s_mtx) s_mtx = xSemaphoreCreateMutex();
    batt_adc_init();
    xTaskCreate(charge_task, "charge", 4096, NULL, 3, NULL);
}

void ha_battery_dump(char *out, size_t outlen)
{
    if (!out || outlen == 0) return;
    size_t n = 0;
    for (int r = 0; r < 4; r++) {
        ha_batt_sample_t s = {0};
        ha_battery_sample(&s);
        n += snprintf(out + n, n < outlen ? outlen - n : 0,
                      "%sbat%dmV(raw%d)/%d%%/usb%dmV/pg%d/chg%d/t%dd%d/chgen%d",
                      r ? " " : "", s.batt_mv, s.raw_ch2, s.soc, s.usb_mv, s.vsys_pg, s.charging,
                      s.have_temp ? s.temp_dc / 10 : -99, s.have_temp ? s.temp_dc % 10 : 0, s.charge_en);
        vTaskDelay(pdMS_TO_TICKS(250));
    }
}
