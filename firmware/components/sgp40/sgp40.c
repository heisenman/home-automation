#include "sgp40.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

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

esp_err_t sgp40_init(sgp40_t *s, i2c_master_bus_handle_t bus, uint32_t scl_hz) {
    i2c_device_config_t dc = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address  = SGP40_I2C_ADDR,
        .scl_speed_hz    = scl_hz,
    };
    return i2c_master_bus_add_device(bus, &dc, &s->dev);
}

// Send a bare 16-bit command, wait, then read `rd_len` bytes (words + CRCs). rd_len 0 = no read.
static esp_err_t cmd_rw(sgp40_t *s, uint16_t cmd, uint32_t delay_ms, uint8_t *rd, int rd_len) {
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

esp_err_t sgp40_self_test(sgp40_t *s) {
    uint8_t r[3];
    esp_err_t e = cmd_rw(s, 0x280E, 320, r, 3);     // measure_test
    if (e != ESP_OK) return e;
    uint16_t res = (uint16_t)((r[0] << 8) | r[1]);
    return (res == 0xD400) ? ESP_OK : ESP_FAIL;     // 0xD400 all-pass, 0x4B00 fail
}

esp_err_t sgp40_measure_raw(sgp40_t *s, uint16_t rh, uint16_t t, uint16_t *sraw_out) {
    uint8_t cmd[8];
    cmd[0] = 0x26; cmd[1] = 0x0F;
    cmd[2] = (uint8_t)(rh >> 8); cmd[3] = (uint8_t)(rh & 0xFF); cmd[4] = crc8(&cmd[2], 2);
    cmd[5] = (uint8_t)(t  >> 8); cmd[6] = (uint8_t)(t  & 0xFF); cmd[7] = crc8(&cmd[5], 2);
    esp_err_t e = i2c_master_transmit(s->dev, cmd, sizeof(cmd), 100);
    if (e != ESP_OK) return e;
    vTaskDelay(pdMS_TO_TICKS(30));                  // measure duration ~30 ms
    uint8_t r[3];
    e = i2c_master_receive(s->dev, r, 3, 100);
    if (e != ESP_OK) return e;
    if (crc8(r, 2) != r[2]) return ESP_ERR_INVALID_CRC;
    *sraw_out = (uint16_t)((r[0] << 8) | r[1]);
    return ESP_OK;
}

esp_err_t sgp40_heater_off(sgp40_t *s) {
    return cmd_rw(s, 0x3615, 0, NULL, 0);
}

esp_err_t sgp40_serial(sgp40_t *s, uint8_t serial[6]) {
    uint8_t r[9];
    esp_err_t e = cmd_rw(s, 0x3682, 1, r, 9);       // 3 words + CRCs
    if (e != ESP_OK) return e;
    serial[0] = r[0]; serial[1] = r[1];
    serial[2] = r[3]; serial[3] = r[4];
    serial[4] = r[6]; serial[5] = r[7];
    return ESP_OK;
}
