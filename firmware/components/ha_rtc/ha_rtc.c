// ha_rtc I2C transport — PCF8563 over the injected i2c_master bus. See ha_rtc.h for the contract.
// Kept deliberately thin: all the BCD/tm logic is in ha_rtc_regs.c (host-tested); this is just the
// register reads/writes. Transactions are short (100 ms) per the datasheet's interface-watchdog note
// (an access held >1 s costs a second of time).
#include "ha_rtc.h"
#include "esp_log.h"

static const char *TAG = "ha_rtc";
static i2c_master_bus_handle_t s_bus;
static i2c_master_dev_handle_t s_dev;
static uint8_t s_addr;

esp_err_t ha_rtc_init(const ha_rtc_cfg_t *cfg)
{
    if (!cfg || !cfg->bus) return ESP_ERR_INVALID_ARG;
    if (s_dev) return ESP_OK;                       // idempotent per boot
    s_bus  = cfg->bus;
    s_addr = cfg->addr ? cfg->addr : HA_RTC_PCF8563_ADDR;
    i2c_device_config_t dc = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address  = s_addr,
        .scl_speed_hz    = 100000,
    };
    esp_err_t err = i2c_master_bus_add_device(s_bus, &dc, &s_dev);
    ESP_LOGI(TAG, "init addr 0x%02x -> %s", s_addr, esp_err_to_name(err));
    return err;
}

bool ha_rtc_present(void)
{
    return s_bus && i2c_master_probe(s_bus, s_addr, 100) == ESP_OK;
}

esp_err_t ha_rtc_get(struct tm *out, bool *valid_out)
{
    if (!s_dev || !out) return ESP_ERR_INVALID_STATE;
    uint8_t reg = HA_RTC_REG_VL_SECS, regs[HA_RTC_NREGS];
    esp_err_t err = i2c_master_transmit_receive(s_dev, &reg, 1, regs, HA_RTC_NREGS, 100);
    if (err != ESP_OK) return err;
    bool vl;
    ha_rtc_regs_to_tm(regs, out, &vl);              // registers freeze during the read -> coherent
    if (valid_out) *valid_out = !vl;
    return ESP_OK;
}

esp_err_t ha_rtc_set(const struct tm *in)
{
    if (!s_dev || !in) return ESP_ERR_INVALID_STATE;
    uint8_t stop_on[2]  = { 0x00, 0x20 };           // Control_status_1: STOP=1 (freeze during write)
    uint8_t stop_off[2] = { 0x00, 0x00 };           // STOP=0 (resume)
    uint8_t buf[1 + HA_RTC_NREGS];
    buf[0] = HA_RTC_REG_VL_SECS;
    ha_rtc_tm_to_regs(in, &buf[1]);                 // BCD + VL cleared + century bit

    esp_err_t err = i2c_master_transmit(s_dev, stop_on, sizeof stop_on, 100);
    if (err == ESP_OK) err = i2c_master_transmit(s_dev, buf, sizeof buf, 100);
    esp_err_t rel = i2c_master_transmit(s_dev, stop_off, sizeof stop_off, 100);  // always release STOP
    return err != ESP_OK ? err : rel;
}
