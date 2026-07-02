# NB: once main declares any REQUIRES, it loses the implicit "depends on every
# component" behaviour — so we must enumerate all of them. `bt` (NimBLE) + esp_hosted
# are the Spike-0 additions; the rest restore what main saw implicitly before.
idf_component_register(SRCS "beachhead_main.c" "bsp_display.c" "ui_tiles.c" "ble_spike.c"
                       INCLUDE_DIRS "."
                       REQUIRES nvs_flash esp_wifi esp_event esp_netif esp_timer mqtt
                                app_update esp_https_ota esp_http_client driver esp_lcd
                                esp_lvgl_port lvgl esp_io_expander esp_io_expander_pca9535
                                esp_lcd_jd9365_8 esp_lcd_touch_gsl3670 mbedtls json esp_adc
                                esp_wifi_remote bt esp_hosted)
