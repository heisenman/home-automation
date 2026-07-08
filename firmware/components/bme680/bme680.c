// BME680 driver + Bosch compensation. See bme680.h. Register map & polynomials per the BME680 datasheet
// and the Bosch BME680_driver reference (BSD-3); float variant.
#include "bme680.h"
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"

static const char *TAG = "bme680";

// ── registers ────────────────────────────────────────────────────────────────────────────────────
#define REG_CHIP_ID      0xD0
#define REG_RESET        0xE0
#define REG_COEFF1       0x89   // 25 bytes
#define REG_COEFF2       0xE1   // 16 bytes
#define REG_RES_HEAT_VAL 0x00
#define REG_RES_HEAT_RNG 0x02   // bits 5:4
#define REG_RANGE_SW_ERR 0x04   // bits 7:4 (signed)
#define REG_CTRL_GAS0    0x70   // heat_off (bit3)
#define REG_CTRL_GAS1    0x71   // run_gas (bit4) | nb_conv (2:0)
#define REG_RES_HEAT0    0x5A
#define REG_GAS_WAIT0    0x64
#define REG_CTRL_HUM     0x72   // osrs_h (2:0)
#define REG_CTRL_MEAS    0x74   // osrs_t(7:5) osrs_p(4:2) mode(1:0)
#define REG_CONFIG       0x75   // filter (4:2)
#define REG_FIELD0       0x1D   // meas_status_0 … gas_r_lsb (15 bytes 0x1D..0x2B)
#define SOFT_RESET_CMD   0xB6

// Forced-mode profile: T 2x, P 16x, H 1x, IIR off, gas heater 320 °C for 150 ms.
#define OSRS_T 0b010
#define OSRS_P 0b101
#define OSRS_H 0b001
#define FILTER 0b000
#define HEATER_TEMP_C 320
#define HEATER_MS     150

#define CONCAT(msb, lsb) (((uint16_t)(msb) << 8) | (uint8_t)(lsb))

static esp_err_t rd(bme680_t *s, uint8_t reg, uint8_t *buf, size_t n) {
    return i2c_master_transmit_receive(s->dev, &reg, 1, buf, n, 1000);
}
static esp_err_t wr(bme680_t *s, uint8_t reg, uint8_t val) {
    uint8_t b[2] = { reg, val };
    return i2c_master_transmit(s->dev, b, 2, 1000);
}

// gas-resistance range LUTs (Bosch, float variant) — the "LUT" step of the interpretation.
static const float K1[16] = {0,0,0,0,0,-1,0,-0.8f,0,0,-0.2f,-0.5f,0,-1,0,0};
static const float K2[16] = {0,0,0,0,0.1f,0.7f,0,-0.8f,-0.1f,0,0,0,0,0,0,0};

static void parse_calib(bme680_t *s, const uint8_t *c /*41 bytes: 0x89..+25 then 0xE1..+16*/) {
    s->par_t1 = CONCAT(c[34], c[33]);
    s->par_t2 = (int16_t)CONCAT(c[2], c[1]);
    s->par_t3 = (int8_t)c[3];
    s->par_p1 = CONCAT(c[6], c[5]);
    s->par_p2 = (int16_t)CONCAT(c[8], c[7]);
    s->par_p3 = (int8_t)c[9];
    s->par_p4 = (int16_t)CONCAT(c[12], c[11]);
    s->par_p5 = (int16_t)CONCAT(c[14], c[13]);
    s->par_p6 = (int8_t)c[16];
    s->par_p7 = (int8_t)c[15];
    s->par_p8 = (int16_t)CONCAT(c[20], c[19]);
    s->par_p9 = (int16_t)CONCAT(c[22], c[21]);
    s->par_p10 = c[23];
    s->par_h1 = (uint16_t)(((uint16_t)c[27] << 4) | (c[26] & 0x0F));
    s->par_h2 = (uint16_t)(((uint16_t)c[25] << 4) | (c[26] >> 4));
    s->par_h3 = (int8_t)c[28];
    s->par_h4 = (int8_t)c[29];
    s->par_h5 = (int8_t)c[30];
    s->par_h6 = c[31];
    s->par_h7 = (int8_t)c[32];
    s->par_gh1 = (int8_t)c[37];
    s->par_gh2 = (int16_t)CONCAT(c[36], c[35]);
    s->par_gh3 = (int8_t)c[38];
}

static uint8_t calc_res_heat(bme680_t *s, uint16_t target_c) {
    if (target_c > 400) target_c = 400;
    float v1 = (s->par_gh1 / 16.0f) + 49.0f;
    float v2 = ((s->par_gh2 / 32768.0f) * 0.0005f) + 0.00235f;
    float v3 = s->par_gh3 / 1024.0f;
    float v4 = v1 * (1.0f + (v2 * target_c));
    float v5 = v4 + (v3 * s->amb_temp_c);
    return (uint8_t)(3.4f * ((v5 * (4.0f / (4.0f + s->res_heat_range)) *
            (1.0f / (1.0f + (s->res_heat_val * 0.002f)))) - 25.0f));
}

static uint8_t calc_gas_wait(uint16_t ms) {
    uint8_t factor = 0;
    if (ms >= 0xFC0) return 0xFF;          // max ~4032 ms
    while (ms > 0x3F) { ms >>= 2; factor++; }
    return (uint8_t)(ms + (factor << 6));
}

esp_err_t bme680_init(bme680_t *s, i2c_master_bus_handle_t bus, uint8_t addr, uint32_t scl_hz) {
    memset(s, 0, sizeof(*s));
    s->amb_temp_c = 25.0f;
    i2c_device_config_t dc = { .dev_addr_length = I2C_ADDR_BIT_LEN_7, .device_address = addr,
                               .scl_speed_hz = scl_hz };
    esp_err_t e = i2c_master_bus_add_device(bus, &dc, &s->dev);
    if (e != ESP_OK) return e;

    uint8_t id = 0;
    if ((e = rd(s, REG_CHIP_ID, &id, 1)) != ESP_OK) return e;
    if (id != BME680_CHIP_ID) { ESP_LOGW(TAG, "chip id 0x%02x != 0x61", id); return ESP_ERR_NOT_FOUND; }

    if ((e = wr(s, REG_RESET, SOFT_RESET_CMD)) != ESP_OK) return e;
    vTaskDelay(pdMS_TO_TICKS(10));

    // Load calibration, RETRY, and VALIDATE BOTH coefficient blocks populated. Right after reset the sensor
    // is still copying NVM into the coeff registers; read too soon and a block comes back all-zero — the I2C
    // read "succeeds" but the missing coeffs then silently yield 0.0 forever. The two blocks can fail
    // INDEPENDENTLY: `par_t1` lives in block 2 (0xE1, with humidity+gas), while `par_t2`/`par_p*` live in
    // block 1 (0x89). Validating only par_t1 (the original bug) passed while block 1 was zero → temperature
    // AND pressure read 0.0 while humidity/gas worked (observed 2026-07-08 on hoffice_c6). So require a
    // coefficient from EACH block to be non-zero (they're never legitimately zero on a real part), and
    // re-read the whole calibration until both are present.
    uint8_t coeff[41];
    for (int tries = 0; ; tries++) {
        if ((e = rd(s, REG_COEFF1, &coeff[0], 25)) != ESP_OK) return e;
        if ((e = rd(s, REG_COEFF2, &coeff[25], 16)) != ESP_OK) return e;
        parse_calib(s, coeff);
        if (s->par_t1 != 0 && s->par_t2 != 0 && s->par_p1 != 0) break;   // block 2 (par_t1) + block 1 (par_t2/p1) both loaded
        if (tries >= 8) { ESP_LOGW(TAG, "calibration incomplete after retries (t1=%u t2=%d p1=%u)",
                                   s->par_t1, s->par_t2, s->par_p1); return ESP_ERR_INVALID_RESPONSE; }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
    uint8_t rhv, rhr, rse;
    if ((e = rd(s, REG_RES_HEAT_VAL, &rhv, 1)) != ESP_OK) return e;
    if ((e = rd(s, REG_RES_HEAT_RNG, &rhr, 1)) != ESP_OK) return e;
    if ((e = rd(s, REG_RANGE_SW_ERR, &rse, 1)) != ESP_OK) return e;
    s->res_heat_val = (int8_t)rhv;
    s->res_heat_range = (rhr & 0x30) >> 4;
    s->range_sw_err = ((int8_t)(rse & 0xF0)) / 16;

    // Program oversampling, filter, and the single gas heater step (profile 0).
    if ((e = wr(s, REG_CTRL_HUM, OSRS_H)) != ESP_OK) return e;
    if ((e = wr(s, REG_CONFIG, FILTER << 2)) != ESP_OK) return e;
    if ((e = wr(s, REG_RES_HEAT0, calc_res_heat(s, HEATER_TEMP_C))) != ESP_OK) return e;
    if ((e = wr(s, REG_GAS_WAIT0, calc_gas_wait(HEATER_MS))) != ESP_OK) return e;
    if ((e = wr(s, REG_CTRL_GAS0, 0x00)) != ESP_OK) return e;      // heater on
    if ((e = wr(s, REG_CTRL_GAS1, 0x10)) != ESP_OK) return e;      // run_gas=1, nb_conv=0
    // Prime ctrl_meas with oversampling in sleep mode; bme680_measure() flips the mode bits to forced.
    return wr(s, REG_CTRL_MEAS, (OSRS_T << 5) | (OSRS_P << 2) | 0x00);
}

esp_err_t bme680_measure(bme680_t *s, bme680_reading_t *out) {
    esp_err_t e;
    // Re-program the heater against the latest ambient estimate, then trigger forced mode.
    if ((e = wr(s, REG_RES_HEAT0, calc_res_heat(s, HEATER_TEMP_C))) != ESP_OK) return e;
    if ((e = wr(s, REG_CTRL_MEAS, (OSRS_T << 5) | (OSRS_P << 2) | 0x01)) != ESP_OK) return e;

    // Wait out T/P/H conversion (~ a few ms) + heater duration, then poll new_data with a bounded retry.
    vTaskDelay(pdMS_TO_TICKS(HEATER_MS + 40));
    uint8_t f[15];
    for (int tries = 0; tries < 10; tries++) {
        if ((e = rd(s, REG_FIELD0, f, sizeof(f))) != ESP_OK) return e;
        if (f[0] & 0x80) break;                    // new_data_0
        vTaskDelay(pdMS_TO_TICKS(10));
    }

    uint32_t press_adc = ((uint32_t)f[2] << 12) | ((uint32_t)f[3] << 4) | (f[4] >> 4); // 0x1F/20/21
    uint32_t temp_adc  = ((uint32_t)f[5] << 12) | ((uint32_t)f[6] << 4) | (f[7] >> 4); // 0x22/23/24
    uint16_t hum_adc   = ((uint16_t)f[8] << 8) | f[9];                                 // 0x25/26
    uint16_t gas_adc   = ((uint16_t)f[13] << 2) | (f[14] >> 6);                        // 0x2A / 0x2B[7:6]
    uint8_t  gas_range = f[14] & 0x0F;
    out->gas_valid     = ((f[14] & 0x20) && (f[14] & 0x10)) ? 1 : 0;                   // gas_valid & heat_stab

    // Temperature (sets t_fine).
    float v1 = ((temp_adc / 16384.0f) - (s->par_t1 / 1024.0f)) * s->par_t2;
    float v2 = (((temp_adc / 131072.0f) - (s->par_t1 / 8192.0f)) *
                ((temp_adc / 131072.0f) - (s->par_t1 / 8192.0f))) * (s->par_t3 * 16.0f);
    s->t_fine = v1 + v2;
    out->temperature_c = s->t_fine / 5120.0f;
    s->amb_temp_c = out->temperature_c;            // feed back into the next heater calc

    // Pressure (Pa -> hPa).
    v1 = (s->t_fine / 2.0f) - 64000.0f;
    v2 = v1 * v1 * (s->par_p6 / 131072.0f);
    v2 = v2 + (v1 * s->par_p5 * 2.0f);
    v2 = (v2 / 4.0f) + (s->par_p4 * 65536.0f);
    v1 = (((s->par_p3 * v1 * v1) / 16384.0f) + (s->par_p2 * v1)) / 524288.0f;
    v1 = (1.0f + (v1 / 32768.0f)) * s->par_p1;
    float p = 1048576.0f - press_adc;
    if (v1 != 0.0f) {
        p = ((p - (v2 / 4096.0f)) * 6250.0f) / v1;
        float pv1 = (s->par_p9 * p * p) / 2147483648.0f;
        float pv2 = p * (s->par_p8 / 32768.0f);
        float pv3 = (p / 256.0f) * (p / 256.0f) * (p / 256.0f) * (s->par_p10 / 131072.0f);
        p = p + (pv1 + pv2 + pv3 + (s->par_p7 * 128.0f)) / 16.0f;
    } else p = 0.0f;
    out->pressure_hpa = p / 100.0f;

    // Humidity.
    float tc = out->temperature_c;
    float hv1 = hum_adc - ((s->par_h1 * 16.0f) + ((s->par_h3 / 2.0f) * tc));
    float hv2 = hv1 * ((s->par_h2 / 262144.0f) * (1.0f + ((s->par_h4 / 16384.0f) * tc) +
                ((s->par_h5 / 1048576.0f) * tc * tc)));
    float hv3 = s->par_h6 / 16384.0f;
    float hv4 = s->par_h7 / 2097152.0f;
    float h = hv2 + ((hv3 + (hv4 * tc)) * hv2 * hv2);
    if (h > 100.0f) h = 100.0f; else if (h < 0.0f) h = 0.0f;
    out->humidity_pct = h;

    // Gas resistance (Ω) via the two range LUTs.
    float gv1 = 1340.0f + (5.0f * s->range_sw_err);
    float gv2 = gv1 * (1.0f + K1[gas_range] / 100.0f);
    float gv3 = 1.0f + (K2[gas_range] / 100.0f);
    out->gas_resistance_ohm =
        1.0f / (gv3 * 0.000000125f * (float)(1 << gas_range) * (((gas_adc - 512.0f) / gv2) + 1.0f));

    return ESP_OK;
}
