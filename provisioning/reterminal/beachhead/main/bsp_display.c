// Lean D1001 display bring-up (ADR-0019 Phase 2). See bsp_display.h.
// Pin map + sequence lifted verbatim from Seeed's BSP
// (esp32_p4_re_terminal_d1001.c) so we inherit their proven timing, minus the
// heavy audio/cam/sensor deps.
#include "bsp_display.h"
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_check.h"
#include "driver/i2c_master.h"
#include "driver/ledc.h"
#include "esp_ldo_regulator.h"
#include "esp_lcd_mipi_dsi.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_io_expander_pca9535.h"
#include "esp_lcd_jd9365_8.h"
#include "esp_lcd_touch_gsl3670.h"
#include "esp_lvgl_port.h"

static const char *TAG = "disp";

// ---- Board pin map (from BSP config.h / esp-bsp.h) ----
#define I2C0_SCL   38   // touch / cam / light
#define I2C0_SDA   37
#define I2C1_SCL   21   // io-expander / codec / rtc
#define I2C1_SDA   20
#define LCD_BL_GPIO 14  // LEDC PWM backlight

// PCA9535 expander pin masks (1ULL<<n)
#define EXP_LCD_PWR_EN   (1ULL << 0)
#define EXP_LCD_RST      (1ULL << 2)
#define EXP_LCD_BL_EN    (1ULL << 7)
#define EXP_PWR_HOLD     (1ULL << 8)   // vdd_3v3 hold
#define EXP_TOUCH_RST    (1ULL << 12)

// MIPI-DSI / panel geometry
#define DSI_LANES        2
#define DSI_LANE_MBPS    1000
#define DSI_LDO_CHAN     3
#define DSI_LDO_MV       2500
#define LCD_H_RES        800
#define LCD_V_RES        1280

#define LEDC_TIMER       LEDC_TIMER_1
#define LEDC_CH          LEDC_CHANNEL_1

static i2c_master_bus_handle_t s_i2c0, s_i2c1;
// GLOBAL (non-static) on purpose: the vendored esp_lcd_touch_gsl3670 driver
// `extern`s this exact symbol to drive the touch-reset line via the expander.
esp_io_expander_handle_t io_expander = NULL;
static esp_lcd_panel_handle_t   s_panel;
static esp_lcd_panel_io_handle_t s_io;
static lv_display_t *s_disp;
static bool s_ready;

static esp_err_t i2c_bus(int port, int scl, int sda, i2c_master_bus_handle_t *out)
{
    i2c_master_bus_config_t c = {
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .i2c_port = port, .scl_io_num = scl, .sda_io_num = sda,
    };
    return i2c_new_master_bus(&c, out);
}

static esp_err_t backlight_init(void)
{
    ledc_timer_config_t t = {
        .speed_mode = LEDC_LOW_SPEED_MODE, .duty_resolution = LEDC_TIMER_10_BIT,
        .timer_num = LEDC_TIMER, .freq_hz = 1000, .clk_cfg = LEDC_AUTO_CLK,
    };
    ESP_RETURN_ON_ERROR(ledc_timer_config(&t), TAG, "ledc timer");
    ledc_channel_config_t ch = {
        .gpio_num = LCD_BL_GPIO, .speed_mode = LEDC_LOW_SPEED_MODE, .channel = LEDC_CH,
        .intr_type = LEDC_INTR_DISABLE, .timer_sel = LEDC_TIMER, .duty = 0, .hpoint = 0,
    };
    return ledc_channel_config(&ch);
}

esp_err_t bsp_display_brightness(int percent)
{
    if (percent < 0) percent = 0;
    if (percent > 100) percent = 100;
    uint32_t duty = (1023 * percent) / 100;
    ESP_RETURN_ON_ERROR(ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CH, duty), TAG, "duty");
    return ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CH);
}

// Power the panel rails via the PCA9535 (leaving the camera OFF).
static esp_err_t power_rails(void)
{
    ESP_RETURN_ON_ERROR(i2c_bus(1, I2C1_SCL, I2C1_SDA, &s_i2c1), TAG, "i2c1");
    ESP_RETURN_ON_ERROR(esp_io_expander_new_i2c_pca9535(
        s_i2c1, ESP_IO_EXPANDER_I2C_PCA9535_ADDRESS_000, &io_expander), TAG, "pca9535");
    ESP_RETURN_ON_ERROR(esp_io_expander_set_dir(io_expander, 0xffff, IO_EXPANDER_OUTPUT), TAG, "exp dir");
    esp_io_expander_set_level(io_expander, EXP_PWR_HOLD, 1);   // hold vdd_3v3
    esp_io_expander_set_level(io_expander, EXP_LCD_BL_EN, 1);  // backlight power
    esp_io_expander_set_level(io_expander, EXP_LCD_PWR_EN, 1); // display power
    esp_io_expander_set_level(io_expander, EXP_LCD_RST, 1);
    esp_io_expander_set_level(io_expander, EXP_TOUCH_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(20));
    return ESP_OK;
}

static esp_err_t panel_init(void)
{
    // MIPI DSI PHY power (LDO_VO3 -> VDD_MIPI_DPHY)
    static esp_ldo_channel_handle_t ldo;
    esp_ldo_channel_config_t ldo_cfg = { .chan_id = DSI_LDO_CHAN, .voltage_mv = DSI_LDO_MV };
    ESP_RETURN_ON_ERROR(esp_ldo_acquire_channel(&ldo_cfg, &ldo), TAG, "ldo");

    esp_lcd_dsi_bus_handle_t dsi;
    esp_lcd_dsi_bus_config_t bus = {
        .bus_id = 0, .num_data_lanes = DSI_LANES,
        .phy_clk_src = MIPI_DSI_PHY_CLK_SRC_DEFAULT, .lane_bit_rate_mbps = DSI_LANE_MBPS,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_dsi_bus(&bus, &dsi), TAG, "dsi bus");

    esp_lcd_dbi_io_config_t dbi = { .virtual_channel = 0, .lcd_cmd_bits = 8, .lcd_param_bits = 8 };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_dbi(dsi, &dbi, &s_io), TAG, "dbi io");

    esp_lcd_dpi_panel_config_t dpi = JD9365_8_800_1280_PANEL_60HZ_DPI_CONFIG(LCD_COLOR_PIXEL_FORMAT_RGB565);
    dpi.num_fbs = 1;  // single PSRAM framebuffer (2 MB) — plenty for a first bring-up
    jd9365_8_vendor_config_t vendor = {
        .mipi_config = { .dsi_bus = dsi, .dpi_config = &dpi, .lane_num = DSI_LANES },
    };
    esp_lcd_panel_dev_config_t dev = {
        .reset_gpio_num = -1,                 // reset is on the expander, pulsed below
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
        .vendor_config = &vendor,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_jd9365_8(s_io, &dev, &s_panel), TAG, "jd9365");

    // HW reset pulse via expander (BSP timing)
    esp_io_expander_set_level(io_expander, EXP_LCD_RST, 1); vTaskDelay(pdMS_TO_TICKS(5));
    esp_io_expander_set_level(io_expander, EXP_LCD_RST, 0); vTaskDelay(pdMS_TO_TICKS(10));
    esp_io_expander_set_level(io_expander, EXP_LCD_RST, 1); vTaskDelay(pdMS_TO_TICKS(120));

    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(s_panel), TAG, "panel init");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_disp_on_off(s_panel, true), TAG, "panel on");
    return ESP_OK;
}

static esp_err_t lvgl_init(void)
{
    lvgl_port_cfg_t pc = ESP_LVGL_PORT_INIT_CONFIG();
    ESP_RETURN_ON_ERROR(lvgl_port_init(&pc), TAG, "lvgl port");

    lvgl_port_display_cfg_t disp = {
        .io_handle = s_io, .panel_handle = s_panel,
        .buffer_size = LCD_H_RES * LCD_V_RES,
        .double_buffer = false,
        .hres = LCD_H_RES, .vres = LCD_V_RES, .monochrome = false,
        .color_format = LV_COLOR_FORMAT_RGB565,
        .rotation = { .swap_xy = false, .mirror_x = false, .mirror_y = false },
        .flags = { .buff_spiram = true, .buff_dma = false, .swap_bytes = false },
    };
    lvgl_port_display_dsi_cfg_t dpi = { .flags = { .avoid_tearing = false } };
    s_disp = lvgl_port_add_disp_dsi(&disp, &dpi);
    return s_disp ? ESP_OK : ESP_FAIL;
}

// Best-effort touch: failure is non-fatal (static UI still works).
static void touch_init(void)
{
    if (i2c_bus(0, I2C0_SCL, I2C0_SDA, &s_i2c0) != ESP_OK) { ESP_LOGW(TAG, "touch i2c skipped"); return; }
    esp_lcd_panel_io_handle_t tio = NULL;
    esp_lcd_panel_io_i2c_config_t io_cfg = ESP_LCD_TOUCH_IO_I2C_GSL3670_CONFIG();
    io_cfg.scl_speed_hz = 400000;
    if (esp_lcd_new_panel_io_i2c(s_i2c0, &io_cfg, &tio) != ESP_OK) { ESP_LOGW(TAG, "touch io skipped"); return; }
    esp_lcd_touch_config_t tc = {
        .x_max = LCD_H_RES, .y_max = LCD_V_RES,
        // NB: the gsl3670 driver treats rst_gpio_num as an EXPANDER pin (1<<rst
        // on the global io_expander), not a real GPIO. 12 = TOUCH_RST.
        .rst_gpio_num = 12, .int_gpio_num = GPIO_NUM_NC,
        .levels = { .reset = 0, .interrupt = 0 },
        .flags = { .swap_xy = 0, .mirror_x = 1, .mirror_y = 1 },
    };
    esp_lcd_touch_handle_t tp = NULL;
    if (esp_lcd_touch_new_i2c_gsl3670(tio, &tc, &tp) != ESP_OK) { ESP_LOGW(TAG, "gsl3670 skipped"); return; }
    const lvgl_port_touch_cfg_t pt = { .disp = s_disp, .handle = tp };
    if (lvgl_port_add_touch(&pt) == NULL) ESP_LOGW(TAG, "lvgl touch skipped");
    else ESP_LOGI(TAG, "touch ready");
}

// Minimal "hello" splash so we can confirm pixels on the bench.
static void splash(void)
{
    if (!lvgl_port_lock(0)) return;
    lv_obj_t *scr = lv_scr_act();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x0b1021), 0);
    lv_obj_t *card = lv_obj_create(scr);
    lv_obj_set_size(card, 520, 260);
    lv_obj_center(card);
    lv_obj_set_style_bg_color(card, lv_color_hex(0x16204a), 0);
    lv_obj_set_style_border_width(card, 0, 0);
    lv_obj_set_style_radius(card, 18, 0);
    lv_obj_t *t = lv_label_create(card);
    lv_label_set_text(t, "reTerminal D1001");
    lv_obj_set_style_text_color(t, lv_color_hex(0xffffff), 0);
    lv_obj_align(t, LV_ALIGN_TOP_MID, 0, 8);
    lv_obj_t *s = lv_label_create(card);
    lv_label_set_text(s, "HA panel  Phase 2\ndisplay online");
    lv_obj_set_style_text_color(s, lv_color_hex(0x8fb4ff), 0);
    lv_obj_set_style_text_align(s, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_center(s);
    lvgl_port_unlock();
}

esp_err_t bsp_display_start(void)
{
    ESP_RETURN_ON_ERROR(backlight_init(), TAG, "backlight");
    ESP_RETURN_ON_ERROR(power_rails(), TAG, "power");
    ESP_RETURN_ON_ERROR(panel_init(), TAG, "panel");
    ESP_RETURN_ON_ERROR(lvgl_init(), TAG, "lvgl");
    touch_init();               // non-fatal
    splash();
    bsp_display_brightness(80);
    s_ready = true;
    ESP_LOGI(TAG, "display ready (%dx%d)", LCD_H_RES, LCD_V_RES);
    return ESP_OK;
}

bool bsp_display_ready(void) { return s_ready; }

bool bsp_display_do(void (*fn)(void *user), void *user)
{
    if (!s_ready || !fn) return false;
    if (!lvgl_port_lock(0)) return false;
    fn(user);
    lvgl_port_unlock();
    return true;
}
