#include "sgp30.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

// CRC-8, poly 0x31, init 0xFF (Sensirion) over one 16-bit data word. Identical to the SGP40's.
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
static esp_err_t cmd_rw(sgp30_t *s, uint16_t cmd, uint32_t delay_ms, uint8_t *rd, int rd_len) {
    uint8_t c[2] = { (uint8_t)(cmd >> 8), (uint8_t)(cmd & 0xFF) };
    esp_err_t e = i2c_master_transmit(s->dev, c, 2, 100);
    if (e != ESP_OK) return e;
    if (delay_ms) vTaskDelay(pdMS_TO_TICKS(delay_ms));
    if (rd_len == 0) return ESP_OK;
    e = i2c_master_receive(s->dev, rd, rd_len, 100);
    if (e != ESP_OK) return e;
    for (int i = 0; i + 2 < rd_len + 1; i += 3)     // validate each word's CRC
        if (crc8(&rd[i], 2) != rd[i + 2]) return ESP_ERR_INVALID_CRC;
    return ESP_OK;
}

esp_err_t sgp30_init(sgp30_t *s, i2c_master_bus_handle_t bus, uint32_t scl_hz) {
    i2c_device_config_t dc = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address  = SGP30_I2C_ADDR,
        .scl_speed_hz    = scl_hz,
    };
    esp_err_t e = i2c_master_bus_add_device(bus, &dc, &s->dev);
    if (e != ESP_OK) return e;
    return cmd_rw(s, 0x2003, 10, NULL, 0);          // Init_air_quality — starts the 1 Hz algorithm
}

esp_err_t sgp30_self_test(sgp30_t *s) {
    uint8_t r[3];
    esp_err_t e = cmd_rw(s, 0x2032, 220, r, 3);     // measure_test
    if (e != ESP_OK) return e;
    uint16_t res = (uint16_t)((r[0] << 8) | r[1]);
    return (res == 0xD400) ? ESP_OK : ESP_FAIL;     // 0xD400 all-pass
}

esp_err_t sgp30_measure(sgp30_t *s, uint16_t *eco2_ppm, uint16_t *tvoc_ppb) {
    uint8_t r[6];
    esp_err_t e = cmd_rw(s, 0x2008, 12, r, 6);      // measure_air_quality: CO2eq word, then TVOC word
    if (e != ESP_OK) return e;
    if (eco2_ppm) *eco2_ppm = (uint16_t)((r[0] << 8) | r[1]);
    if (tvoc_ppb) *tvoc_ppb = (uint16_t)((r[3] << 8) | r[4]);
    return ESP_OK;
}

esp_err_t sgp30_get_baseline(sgp30_t *s, uint16_t *eco2_base, uint16_t *tvoc_base) {
    uint8_t r[6];
    esp_err_t e = cmd_rw(s, 0x2015, 10, r, 6);      // get_iaq_baseline: CO2eq baseline, then TVOC baseline
    if (e != ESP_OK) return e;
    if (eco2_base) *eco2_base = (uint16_t)((r[0] << 8) | r[1]);
    if (tvoc_base) *tvoc_base = (uint16_t)((r[3] << 8) | r[4]);
    return ESP_OK;
}

esp_err_t sgp30_set_baseline(sgp30_t *s, uint16_t eco2_base, uint16_t tvoc_base) {
    // set_iaq_baseline (0x201E) takes the two words in the OPPOSITE order to get_iaq_baseline:
    // TVOC baseline FIRST, then eCO2 baseline (Sensirion datasheet quirk).
    uint8_t cmd[8];
    cmd[0] = 0x20; cmd[1] = 0x1E;
    cmd[2] = (uint8_t)(tvoc_base >> 8); cmd[3] = (uint8_t)(tvoc_base & 0xFF); cmd[4] = crc8(&cmd[2], 2);
    cmd[5] = (uint8_t)(eco2_base >> 8); cmd[6] = (uint8_t)(eco2_base & 0xFF); cmd[7] = crc8(&cmd[5], 2);
    return i2c_master_transmit(s->dev, cmd, sizeof(cmd), 100);
}

esp_err_t sgp30_serial(sgp30_t *s, uint8_t serial[6]) {
    uint8_t r[9];
    esp_err_t e = cmd_rw(s, 0x3682, 1, r, 9);       // 3 words + CRCs
    if (e != ESP_OK) return e;
    serial[0] = r[0]; serial[1] = r[1];
    serial[2] = r[3]; serial[3] = r[4];
    serial[4] = r[6]; serial[5] = r[7];
    return ESP_OK;
}
