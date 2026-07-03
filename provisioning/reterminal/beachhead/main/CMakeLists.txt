# NB: once main declares any REQUIRES, it loses the implicit "depends on every
# component" behaviour — so we must enumerate all of them. `bt` (NimBLE) + esp_hosted
# are the Spike-0 additions; the rest restore what main saw implicitly before.
idf_component_register(SRCS "beachhead_main.c" "bsp_display.c" "ui_tiles.c" "ui/ui_format.c" "ui/ui_http.c" "ui/ui_chart.c" "ui/ui_grid.c" "ui/ui_expand.c" "ui/ui_admin.c" "ui/ui_scenes.c" "ui/ui_controls.c" "lv_mem_psram.c" "bat_profile.c"
                       INCLUDE_DIRS "."
                       REQUIRES nvs_flash esp_wifi esp_event esp_netif esp_timer mqtt
                                app_update esp_https_ota esp_http_client driver esp_lcd
                                esp_lvgl_port lvgl esp_io_expander esp_io_expander_pca9535
                                esp_lcd_jd9365_8 esp_lcd_touch_gsl3670 mbedtls json
                                esp_wifi_remote bt esp_hosted
                                switchbot_decode ha_ble_scan fs_ops ha_sdcard ha_battery ha_reach)

# lv_mem_psram.c defines LVGL's custom allocator core (lv_malloc_core/lv_free_core/…). Those
# symbols are referenced only by lvgl.a (cross-archive), so static link-order would drop the
# object from libmain.a before LVGL's undefined refs appear. Force-link it: pulling one symbol
# pulls the whole object member, resolving all the lv_*_core functions.
target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,-u,lv_malloc_core")
