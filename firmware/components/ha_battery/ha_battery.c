// Battery gauge + thermal-gated charging — see ha_battery.h.
#include "ha_battery.h"

#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "driver/gpio.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"

static const char *TAG = "ha_batt";

#define ADC_SAMPLES 16
#define AVG_WIN      8

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
        .usb_present_mv = 4000, .volt_high_mv = 4150, .volt_recharge_mv = 4050,
        .temp_min_dc = 20, .temp_max_dc = 430,
    };
    return c;
}

static ha_battery_cfg_t s_cfg;
static bool s_have_cfg;
static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_cali_batt, s_cali_usb;
static bool s_init;
static bool s_read_en;
static int  s_smooth_mv;
static int  s_win[AVG_WIN], s_win_n, s_win_i;
static i2c_master_dev_handle_t s_imu;
static bool s_imu_ok, s_imu_tried;
static volatile bool s_charge_en;
static SemaphoreHandle_t s_mtx;

// LSM6DS3 registers
#define LSM6_WHOAMI    0x0F
#define LSM6_CTRL1_XL  0x10
#define LSM6_OUT_TEMP  0x20

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
    adc_oneshot_unit_init_cfg_t uc = { .unit_id = s_cfg.adc_unit, .ulp_mode = ADC_ULP_MODE_DISABLE };
    if (adc_oneshot_new_unit(&uc, &s_adc) != ESP_OK) { ESP_LOGW(TAG, "adc unit fail"); return; }
    adc_oneshot_chan_cfg_t cc = { .atten = s_cfg.adc_atten, .bitwidth = ADC_BITWIDTH_12 };
    adc_oneshot_config_channel(s_adc, s_cfg.adc_ch_batt, &cc);
    if (s_cfg.adc_ch_usb >= 0) adc_oneshot_config_channel(s_adc, s_cfg.adc_ch_usb, &cc);
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
    s_init = true;
}

static int lut_pct(int mv)
{
    const int *L = s_cfg.lut; int n = s_cfg.lut_n;
    if (mv < L[0]) return 0;
    for (int i = 1; i < n; i++)
        if (mv < L[i]) return (i - 1) * 5 + 5 * (mv - L[i-1]) / (L[i] - L[i-1]);
    return 100;
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

static void imu_init(void)
{
    if (s_imu_tried) return;
    if (!s_cfg.i2c_bus || !s_cfg.imu_addr) { s_imu_tried = true; return; }
    s_imu_tried = true;
    i2c_device_config_t dc = { .dev_addr_length = I2C_ADDR_BIT_LEN_7,
                               .device_address = s_cfg.imu_addr, .scl_speed_hz = 400000 };
    if (i2c_master_bus_add_device(s_cfg.i2c_bus, &dc, &s_imu) != ESP_OK) { s_imu = NULL; return; }
    uint8_t reg = LSM6_WHOAMI, who = 0;
    if (i2c_master_transmit_receive(s_imu, &reg, 1, &who, 1, 100) != ESP_OK || who != s_cfg.imu_addr) {
        ESP_LOGW(TAG, "IMU whoami=0x%02x (want 0x%02x) — temp gating off", who, s_cfg.imu_addr);
        return;
    }
    uint8_t c1[2] = { LSM6_CTRL1_XL, 0x10 };   // ODR 12.5Hz → temp register updates
    i2c_master_transmit(s_imu, c1, 2, 100);
    s_imu_ok = true;
}

static bool imu_temp_dc(int *dc)
{
    imu_init();
    if (!s_imu_ok) return false;
    uint8_t reg = LSM6_OUT_TEMP, b[2] = { 0, 0 };
    if (i2c_master_transmit_receive(s_imu, &reg, 1, b, 2, 100) != ESP_OK) return false;
    int16_t raw = (int16_t)((b[1] << 8) | b[0]);
    *dc = (raw * 10) / 256 + 250;   // deci-°C = raw/256*10 + 25.0*10
    return true;
}

esp_err_t ha_battery_init(const ha_battery_cfg_t *cfg)
{
    if (!cfg) return ESP_ERR_INVALID_ARG;
    s_cfg = *cfg;
    if (!s_cfg.lut) { s_cfg.lut = D1001_LUT; s_cfg.lut_n = 21; }
    s_have_cfg = true;
    if (!s_mtx) s_mtx = xSemaphoreCreateMutex();
    batt_adc_init();
    return s_adc ? ESP_OK : ESP_FAIL;
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
    if (s_smooth_mv == 0)        s_smooth_mv = avg;
    else if (on_charger)         s_smooth_mv = avg;
    else if (avg < s_smooth_mv)  s_smooth_mv = avg;   // discharging: ratchet down only

    int tdc = 0; bool ht = imu_temp_dc(&tdc);
    if (out) {
        out->raw_ch2 = raw2;
        out->cali_mv = cali_mv;
        out->batt_mv = batt_mv;
        out->batt_mv_smoothed = s_smooth_mv;
        out->usb_mv = usb_mv;
        out->vsys_pg = (s_cfg.gpio_vsys_pg >= 0) ? gpio_get_level(s_cfg.gpio_vsys_pg) : -1;
        out->charging = (s_cfg.gpio_charge >= 0) && (gpio_get_level(s_cfg.gpio_charge) == 0);
        out->soc = lut_pct(s_smooth_mv);
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

// Thermal-gated charge manager + restart watchdog. Charging stays OFF unless the cable is in,
// the cell is below full, and the board temp is in range (fail-safe: no temp ⇒ no charge). The
// watchdog handles a charger IC that latched "done" early: if we want charge, the cell is well
// below full, yet the IC reports idle, pulse CHARGE_EN off→on to start a fresh cycle.
static void charge_task(void *pv)
{
    charge_set(false);
    s_charge_en = false;
    int stalled = 0;
    for (;;) {
        ha_batt_sample_t s = {0};
        bool ok = (ha_battery_sample(&s) == ESP_OK);
        bool cable   = ok && (s.usb_mv > s_cfg.usb_present_mv);
        bool temp_ok = s.have_temp && (s.temp_dc >= s_cfg.temp_min_dc && s.temp_dc <= s_cfg.temp_max_dc);
        bool room    = ok && (s.batt_mv < s_cfg.volt_high_mv);
        bool want    = cable && temp_ok && room;
        if (want != s_charge_en) {
            charge_set(want);
            s_charge_en = want;
            stalled = 0;
            ESP_LOGW(TAG, "charge %s (usb=%dmV batt=%dmV temp=%d.%d°C)", want ? "ON" : "OFF",
                     s.usb_mv, s.batt_mv, s.have_temp ? s.temp_dc / 10 : -99,
                     s.have_temp ? s.temp_dc % 10 : 0);
        }
        // watchdog: want charge, cell clearly not full, but the IC is idle → kick it
        if (want && !s.charging && s.batt_mv < s_cfg.volt_recharge_mv) {
            if (++stalled >= 5) {   // ~15 s of wanting-but-idle
                ESP_LOGW(TAG, "charger idle at %dmV — kicking CHARGE_EN", s.batt_mv);
                charge_set(false); vTaskDelay(pdMS_TO_TICKS(1500)); charge_set(true);
                stalled = 0;
            }
        } else {
            stalled = 0;
        }
        vTaskDelay(pdMS_TO_TICKS(3000));
    }
}

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
