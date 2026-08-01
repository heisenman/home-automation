#include "sgp41.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

// Command codes (SGP41 datasheet Table 8). 0x202F is deliberately NOT in that table — see sgp4x_identify.
#define CMD_CONDITION     0x2612
#define CMD_MEASURE_RAW   0x2619
#define CMD_SELF_TEST     0x280E
#define CMD_HEATER_OFF    0x3615
#define CMD_SERIAL        0x3682
#define CMD_FEATURESET    0x202F
#define CMD_SGP40_MEASURE 0x260F   // the SGP40's measure_raw — used only as a negative probe

#define SGP40_FEATURESET  0x0020   // low 9 bits of 0x202F
#define SGP41_FEATURESET  0x0040

// CRC-8, poly 0x31, init 0xFF (Sensirion) over one 16-bit data word.
static uint8_t crc8(const uint8_t *d, int n) {
    uint8_t c = 0xFF;
    for (int i = 0; i < n; i++) {
        c ^= d[i];
        for (int b = 0; b < 8; b++)
            c = (c & 0x80) ? (uint8_t)((c << 1) ^ 0x31) : (uint8_t)(c << 1);
    }
    return c;
}

// Send a bare 16-bit command, wait, then read `rd_len` bytes (words + CRCs). rd_len 0 = no read.
static esp_err_t cmd_rw(i2c_master_dev_handle_t dev, uint16_t cmd, uint32_t delay_ms,
                        uint8_t *rd, int rd_len) {
    uint8_t c[2] = { (uint8_t)(cmd >> 8), (uint8_t)(cmd & 0xFF) };
    esp_err_t e = i2c_master_transmit(dev, c, 2, 100);
    if (e != ESP_OK) return e;
    if (delay_ms) vTaskDelay(pdMS_TO_TICKS(delay_ms));
    if (rd_len == 0) return ESP_OK;
    e = i2c_master_receive(dev, rd, rd_len, 100);
    if (e != ESP_OK) return e;
    for (int i = 0; i + 2 < rd_len + 1; i += 3)     // validate each word's CRC
        if (crc8(&rd[i], 2) != rd[i + 2]) return ESP_ERR_INVALID_CRC;
    return ESP_OK;
}

// Send a command that carries the 6-byte RH/T parameter block (conditioning and measurement both do),
// then read `rd_len` bytes. Shared by sgp41_condition, sgp41_measure_raw and the identify probes.
static esp_err_t cmd_param_rw(i2c_master_dev_handle_t dev, uint16_t cmd, uint16_t rh, uint16_t t,
                              uint32_t delay_ms, uint8_t *rd, int rd_len) {
    uint8_t c[8];
    c[0] = (uint8_t)(cmd >> 8); c[1] = (uint8_t)(cmd & 0xFF);
    c[2] = (uint8_t)(rh >> 8);  c[3] = (uint8_t)(rh & 0xFF);  c[4] = crc8(&c[2], 2);
    c[5] = (uint8_t)(t  >> 8);  c[6] = (uint8_t)(t  & 0xFF);  c[7] = crc8(&c[5], 2);
    esp_err_t e = i2c_master_transmit(dev, c, sizeof(c), 100);
    if (e != ESP_OK) return e;
    if (delay_ms) vTaskDelay(pdMS_TO_TICKS(delay_ms));
    if (rd_len == 0) return ESP_OK;
    e = i2c_master_receive(dev, rd, rd_len, 100);
    if (e != ESP_OK) return e;
    for (int i = 0; i + 2 < rd_len + 1; i += 3)
        if (crc8(&rd[i], 2) != rd[i + 2]) return ESP_ERR_INVALID_CRC;
    return ESP_OK;
}

static esp_err_t add_dev(i2c_master_bus_handle_t bus, uint32_t scl_hz, i2c_master_dev_handle_t *out) {
    i2c_device_config_t dc = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address  = SGP41_I2C_ADDR,
        .scl_speed_hz    = scl_hz,
    };
    return i2c_master_bus_add_device(bus, &dc, out);
}

esp_err_t sgp41_init(sgp41_t *s, i2c_master_bus_handle_t bus, uint32_t scl_hz) {
    return add_dev(bus, scl_hz, &s->dev);
}

sgp4x_part_t sgp4x_identify(i2c_master_bus_handle_t bus, uint32_t scl_hz, uint16_t *featureset_out) {
    if (featureset_out) *featureset_out = 0;
    i2c_master_dev_handle_t dev;
    if (add_dev(bus, scl_hz, &dev) != ESP_OK) return SGP4X_PART_UNKNOWN;

    sgp4x_part_t part = SGP4X_PART_UNKNOWN;
    uint8_t r[6];

    // (2) Corroborating read first — it is cheap, harmless, and leaves the part in idle. Undocumented on
    // both parts, so a failure here is expected and MUST NOT be treated as absence of the sensor.
    uint16_t fs = 0;
    if (cmd_rw(dev, CMD_FEATURESET, 2, r, 3) == ESP_OK) {
        fs = (uint16_t)((r[0] << 8) | r[1]);
        if (featureset_out) *featureset_out = fs;
    }

    // (1) Authoritative behavioural probe. An SGP41 answers 0x2619 with two CRC-valid words; an SGP40
    // has no such command and NAKs it. Requiring BOTH the ACK and two good CRCs means a part that
    // happens not to NAK still can't be mistaken for an SGP41 — it would have to fabricate valid CRCs.
    if (cmd_param_rw(dev, CMD_MEASURE_RAW, SGP41_DEFAULT_RH, SGP41_DEFAULT_T, 55, r, 6) == ESP_OK) {
        part = SGP4X_PART_SGP41;
    } else if (cmd_param_rw(dev, CMD_SGP40_MEASURE, SGP41_DEFAULT_RH, SGP41_DEFAULT_T, 35, r, 3) == ESP_OK) {
        part = SGP4X_PART_SGP40;
    } else if ((fs & 0x01FF) == SGP41_FEATURESET) {   // both probes failed — fall back to the featureset
        part = SGP4X_PART_SGP41;
    } else if ((fs & 0x01FF) == SGP40_FEATURESET) {
        part = SGP4X_PART_SGP40;
    }

    // The probes switched a hotplate on. Return to idle so the caller can start the conditioning
    // sequence from the state the datasheet requires.
    cmd_rw(dev, CMD_HEATER_OFF, 1, NULL, 0);
    i2c_master_bus_rm_device(dev);
    return part;
}

esp_err_t sgp41_self_test(sgp41_t *s, uint16_t *result_out) {
    uint8_t r[3];
    esp_err_t e = cmd_rw(s->dev, CMD_SELF_TEST, 320, r, 3);
    if (e != ESP_OK) return e;
    uint16_t res = (uint16_t)((r[0] << 8) | r[1]);
    if (result_out) *result_out = res;
    // Table 15: ignore the high byte; low nibble bit 0 = VOC pixel, bit 1 = NOx pixel, 0 = passed.
    return (res & 0x0003) == 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t sgp41_condition(sgp41_t *s, uint16_t rh, uint16_t t, uint16_t *sraw_voc_out) {
    uint8_t r[3];
    esp_err_t e = cmd_param_rw(s->dev, CMD_CONDITION, rh, t, 50, r, 3);
    if (e != ESP_OK) return e;
    if (sraw_voc_out) *sraw_voc_out = (uint16_t)((r[0] << 8) | r[1]);
    return ESP_OK;
}

esp_err_t sgp41_measure_raw(sgp41_t *s, uint16_t rh, uint16_t t,
                            uint16_t *sraw_voc_out, uint16_t *sraw_nox_out) {
    uint8_t r[6];
    esp_err_t e = cmd_param_rw(s->dev, CMD_MEASURE_RAW, rh, t, 55, r, 6);
    if (e != ESP_OK) return e;
    if (sraw_voc_out) *sraw_voc_out = (uint16_t)((r[0] << 8) | r[1]);   // datasheet Table 13: VOC first,
    if (sraw_nox_out) *sraw_nox_out = (uint16_t)((r[3] << 8) | r[4]);   // then NOx
    return ESP_OK;
}

esp_err_t sgp41_heater_off(sgp41_t *s) {
    return cmd_rw(s->dev, CMD_HEATER_OFF, 0, NULL, 0);
}

esp_err_t sgp41_serial(sgp41_t *s, uint8_t serial[6]) {
    uint8_t r[9];
    esp_err_t e = cmd_rw(s->dev, CMD_SERIAL, 1, r, 9);      // 3 words + CRCs
    if (e != ESP_OK) return e;
    serial[0] = r[0]; serial[1] = r[1];
    serial[2] = r[3]; serial[3] = r[4];
    serial[4] = r[6]; serial[5] = r[7];
    return ESP_OK;
}
