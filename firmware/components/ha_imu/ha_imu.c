// ha_imu I2C transport — LSM6DS3TR-C over the injected i2c_master bus. See ha_imu.h.
// Thin: the unit conversions live in ha_imu_regs.c (host-tested); this owns the register I/O + the
// wake/tap engine setup. Register addresses + bit positions grounded in docs/hardware/lsm6ds3tr-c.pdf.
#include "ha_imu.h"
#include "esp_log.h"

static const char *TAG = "ha_imu";

// --- register map (LSM6DS3TR-C §8, Table 19) ---
#define R_WHOAMI     0x0F   // == 0x6A
#define R_CTRL1_XL   0x10   // accel ODR[7:4] / FS[3:2]
#define R_CTRL3_C    0x12   // BDU(6), IF_INC(2)
#define R_WAKE_SRC   0x1B   // WU_IA = bit3
#define R_TAP_SRC    0x1C   // TAP_IA = bit6, SINGLE_TAP = bit5
#define R_OUT_TEMP   0x20   // 16-bit LE
#define R_OUTX_XL    0x28   // X/Y/Z accel, 6 bytes LE
#define R_TAP_CFG    0x58   // INTERRUPTS_ENABLE(7), SLOPE_FDS(4), TAP_X/Y/Z_EN(3:1), LIR(0)
#define R_TAP_THS_6D 0x59   // TAP_THS[4:0]
#define R_INT_DUR2   0x5A   // tap DUR/QUIET/SHOCK timing
#define R_WAKE_THS   0x5B   // SINGLE_DOUBLE_TAP(7), WK_THS[5:0]
#define R_WAKE_DUR   0x5C   // WAKE_DUR[6:5]
#define R_MD1_CFG    0x5E   // INT1_SINGLE_TAP(6), INT1_WU(5)
#define WHOAMI_VAL   0x6A

static i2c_master_dev_handle_t s_dev;
static bool s_ok;

static esp_err_t wr(uint8_t reg, uint8_t val) { uint8_t b[2] = { reg, val }; return i2c_master_transmit(s_dev, b, 2, 100); }
static esp_err_t rd(uint8_t reg, uint8_t *buf, int n) { return i2c_master_transmit_receive(s_dev, &reg, 1, buf, n, 100); }

esp_err_t ha_imu_init(const ha_imu_cfg_t *cfg)
{
    if (!cfg || !cfg->bus) return ESP_ERR_INVALID_ARG;
    if (s_dev) return ESP_OK;                          // idempotent: one physical chip, one config
    i2c_device_config_t dc = { .dev_addr_length = I2C_ADDR_BIT_LEN_7,
                               .device_address = cfg->addr ? cfg->addr : HA_IMU_LSM6_ADDR,
                               .scl_speed_hz = 400000 };
    if (i2c_master_bus_add_device(cfg->bus, &dc, &s_dev) != ESP_OK) { s_dev = NULL; return ESP_FAIL; }
    uint8_t who = 0;
    if (rd(R_WHOAMI, &who, 1) != ESP_OK || who != WHOAMI_VAL) {
        ESP_LOGW(TAG, "whoami=0x%02x (want 0x%02x) — IMU unavailable", who, WHOAMI_VAL);
        return ESP_ERR_NOT_FOUND;
    }
    wr(R_CTRL3_C, 0x44);   // BDU=1 (coherent multi-byte read) + IF_INC=1 (auto-increment)
    wr(R_CTRL1_XL, 0x20);  // ODR 26 Hz, FS ±2g — temp updates + responsive-enough for wake
    s_ok = true;
    ESP_LOGI(TAG, "init ok (LSM6DS3TR-C @0x%02x)", dc.device_address);
    return ESP_OK;
}

bool ha_imu_present(void) { return s_ok; }

esp_err_t ha_imu_temp_dc(int *dc)
{
    if (!s_ok || !dc) return ESP_ERR_INVALID_STATE;
    uint8_t b[2];
    esp_err_t e = rd(R_OUT_TEMP, b, 2);
    if (e != ESP_OK) return e;
    *dc = ha_imu_temp_raw_to_dc((int16_t)((b[1] << 8) | b[0]));
    return ESP_OK;
}

esp_err_t ha_imu_accel_mg(int *x, int *y, int *z)
{
    if (!s_ok) return ESP_ERR_INVALID_STATE;
    uint8_t b[6];
    esp_err_t e = rd(R_OUTX_XL, b, 6);
    if (e != ESP_OK) return e;
    if (x) *x = ha_imu_accel_raw_to_mg((int16_t)((b[1] << 8) | b[0]));
    if (y) *y = ha_imu_accel_raw_to_mg((int16_t)((b[3] << 8) | b[2]));
    if (z) *z = ha_imu_accel_raw_to_mg((int16_t)((b[5] << 8) | b[4]));
    return ESP_OK;
}

esp_err_t ha_imu_events_enable(int wake_ths_mg)
{
    if (!s_ok) return ESP_ERR_INVALID_STATE;
    wr(R_WAKE_THS, ha_imu_wu_ths_from_mg(wake_ths_mg) & 0x3f);  // WK_THS; SINGLE_DOUBLE_TAP=0 (single only)
    wr(R_WAKE_DUR, 0x00);                                       // WAKE_DUR=0 (fire on first over-threshold)
    wr(R_TAP_THS_6D, 0x0c);                                     // TAP_THS ~12 (≈750 mg)
    wr(R_INT_DUR2, 0x7f);                                       // generous tap DUR/QUIET/SHOCK window
    wr(R_TAP_CFG, 0x8f);   // INTERRUPTS_ENABLE(7) | TAP_X/Y/Z_EN(3:1) | LIR(0) — latch until src read
    wr(R_MD1_CFG, 0x60);   // route wake (INT1_WU b5) + single-tap (INT1_SINGLE_TAP b6) to INT1 / 6D_INTn
    ESP_LOGI(TAG, "wake+tap engine enabled (wake_ths %d mg)", wake_ths_mg);
    return ESP_OK;
}

esp_err_t ha_imu_poll(bool *motion, bool *tap)
{
    if (!s_ok) return ESP_ERR_INVALID_STATE;
    uint8_t ws = 0, ts = 0;
    esp_err_t e = rd(R_WAKE_SRC, &ws, 1);
    if (e != ESP_OK) return e;
    e = rd(R_TAP_SRC, &ts, 1);
    if (e != ESP_OK) return e;
    if (motion) *motion = (ws & 0x08) != 0;   // WAKE_UP_SRC.WU_IA (bit3)
    if (tap)    *tap    = (ts & 0x20) != 0;    // TAP_SRC.SINGLE_TAP (bit5)
    return ESP_OK;
}
