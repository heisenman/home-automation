// Server-backed LVGL tile renderer (ADR-0019 Phase 2). See ui_tiles.h.
//
// Slim orchestrator (ADR-0020 module-first): owns the fetch loop, the MQTT-state fan-in, the
// battery indicator, and screen assembly. Everything else lives in ui/*: metric formatting
// (ui_format), HTTP transport (ui_http), the sensor grid (ui_grid), inline chart expansions
// (ui_expand + ui_chart), the admin session + top-bar chrome (ui_admin), the scene selector
// (ui_scenes), and the actuator command overlay (ui_controls). This file wires them together.
#include "ui_tiles.h"
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "cJSON.h"
#include "lvgl.h"
#include "esp_lvgl_port.h"
#include "ui/ui_format.h"   // shared metric catalog spec (ADR-0020)
#include "ui/ui_http.h"     // GET/POST/send_json transport + BFF base URL (ADR-0020)
#include "ui/ui_expand.h"   // inline expansion stack container (ADR-0020)
#include "ui/ui_chart.h"    // 72h chart fetch worker (ADR-0020)
#include "ui/ui_grid.h"     // sensor tile grid + card registry + live-state patching (ADR-0020)
#include "ui/ui_map.h"      // house map: /api/v1/rooms -> spatial room chips (panel-ui-spatial-nav)
#include "ui/ui_admin.h"    // admin session + top-bar toast/gear + idle auto-lock (ADR-0020)
#include "ui/ui_scenes.h"   // top-bar row + whole-house scene selector (ADR-0020)
#include "ui/ui_controls.h" // actuator cards + command overlay (ADR-0020)
#include "ui/ui_power.h"    // top-bar power-off button + confirm (ADR-0020)
#include "ui/ui_clock.h"    // top-bar wall clock (ADR-0020; roadmap #1, ability G)

static const char *TAG = "ui";
#define REFRESH_MS 10000

static char s_url[192];       // /api/v1/sensors
static char s_disp_url[192];  // /api/v1/displays (controllable devices)
static char s_house_url[256]; // /api/v1/house
static char s_map_url[256];   // /api/v1/rooms (house map — the landing view)
static lv_obj_t *s_header, *s_grid;
static lv_obj_t *s_map;       // house-map container (absolute-positioned room chips)
static lv_obj_t *s_back;      // "< Rooms" back button (shown only in the room-zoom view)
static lv_obj_t *s_batt_lbl;  // battery indicator in the top bar

static char s_room[28];            // selected room slug for room-zoom; "" = house map (landing)
static char s_room_name[28];       // its display name (for the title label)
static volatile bool s_nav_dirty;  // set on a nav change -> ui_task re-renders promptly (no 10s wait)

// House-map room tap -> enter that room's zoom view (the device grid filtered to this room).
static void on_room_tap(const char *area_id, const char *name)
{
    if (!area_id) return;
    strncpy(s_room, area_id, sizeof s_room - 1);
    s_room[sizeof s_room - 1] = 0;
    strncpy(s_room_name, (name && name[0]) ? name : area_id, sizeof s_room_name - 1);
    s_room_name[sizeof s_room_name - 1] = 0;
    s_nav_dirty = true;
    ESP_LOGI(TAG, "nav -> room %s", s_room);
}

// Back button in the room-zoom view -> return to the house map.
static void on_back_clicked(lv_event_t *e)
{
    (void)e;
    s_room[0] = 0;
    s_nav_dirty = true;
}

static bool s_started;
static QueueHandle_t s_state_q;    // MQTT state payloads (char*) -> state_task (LVGL off the mqtt stack)

// True if a sensor/display entry belongs to `filter` (a room slug); filter NULL/"" matches all.
static bool room_match(cJSON *e, const char *filter)
{
    if (!filter || !filter[0]) return true;
    cJSON *jr = cJSON_GetObjectItem(e, "room");
    return cJSON_IsString(jr) && strcmp(jr->valuestring, filter) == 0;
}

// Render the sensor/actuator grid. `room_filter` NULL/"" = whole house (offline fallback); else only
// that room's devices (room-zoom). `title` names the view in the header (room name / "House").
static void render(cJSON *sensors, cJSON *devices, cJSON *catalog, const char *room_filter, const char *title)
{
    if (!lvgl_port_lock(0)) return;
    ui_format_load_catalog(catalog);  // refresh the shared metric spec before rebuilding cards
    ui_grid_reset();                  // free sensor-card details + reset the grid registry
    ui_controls_reset();              // actuator registry rebuilt from scratch each fetch
    lv_obj_clean(s_grid);             // wipe both actuator + sensor card widgets from the shared grid
    int ns = 0, na = 0;
    cJSON *e;
    if (devices) {
        cJSON_ArrayForEach(e, devices) {
            if (room_match(e, room_filter)) { ui_controls_add_card(e, s_grid); na++; }   // actuators first
        }
    }
    if (sensors) {
        cJSON_ArrayForEach(e, sensors) {
            if (room_match(e, room_filter)) { ui_grid_add_card(e, s_grid); ns++; }
        }
    }
    lv_label_set_text_fmt(s_header, "%s  -  %d dev  %d sen", title ? title : "House", na, ns);
    lvgl_port_unlock();
    ESP_LOGI(TAG, "rendered %d actuators + %d sensors (room=%s)", na, ns, room_filter ? room_filter : "all");
}

// Fetch + render one cycle for the CURRENT nav state (house map, or a room's filtered grid).
static void do_render_cycle(void)
{
    if (s_room[0]) {
        // ── room-zoom: the device grid filtered to the selected room ──
        int l1 = 0, l2 = 0;
        char *b1 = ui_http_get(s_url, &l1);         // sensors
        char *b2 = ui_http_get(s_disp_url, &l2);    // controllable devices
        cJSON *r1 = (b1 && l1 > 0) ? cJSON_Parse(b1) : NULL;
        cJSON *r2 = (b2 && l2 > 0) ? cJSON_Parse(b2) : NULL;
        cJSON *sensors = r1 ? cJSON_GetObjectItem(r1, "sensors") : NULL;
        cJSON *devices = r2 ? cJSON_GetObjectItem(r2, "devices") : NULL;
        cJSON *catalog = r1 ? cJSON_GetObjectItem(r1, "metrics") : NULL;
        if (lvgl_port_lock(0)) {
            lv_obj_clear_flag(s_grid, LV_OBJ_FLAG_HIDDEN);
            lv_obj_add_flag(s_map, LV_OBJ_FLAG_HIDDEN);
            lv_obj_clear_flag(s_back, LV_OBJ_FLAG_HIDDEN);   // back button beside the title
            lvgl_port_unlock();
        }
        render(cJSON_IsArray(sensors) ? sensors : NULL, cJSON_IsArray(devices) ? devices : NULL,
               catalog, s_room, s_room_name);
        if (r1) cJSON_Delete(r1);
        if (r2) cJSON_Delete(r2);
        if (b1) heap_caps_free(b1);
        if (b2) heap_caps_free(b2);
    } else {
        // ── house map (landing) ──
        int lm = 0;
        char *bm = ui_http_get(s_map_url, &lm);
        cJSON *rm = (bm && lm > 0) ? cJSON_Parse(bm) : NULL;
        cJSON *rooms = rm ? cJSON_GetObjectItem(rm, "rooms") : NULL;
        bool mapped = false;
        if (cJSON_IsArray(rooms) && cJSON_GetArraySize(rooms) > 0) {
            if (lvgl_port_lock(0)) {
                ui_map_render(rm, s_map, on_room_tap);
                lv_obj_clear_flag(s_map, LV_OBJ_FLAG_HIDDEN);
                lv_obj_add_flag(s_grid, LV_OBJ_FLAG_HIDDEN);
                lv_obj_add_flag(s_back, LV_OBJ_FLAG_HIDDEN);
                lv_label_set_text(s_header, "House");
                lvgl_port_unlock();
            }
            mapped = true;
        }
        if (rm) cJSON_Delete(rm);
        if (bm) heap_caps_free(bm);

        if (!mapped) {
            // fallback: the flat whole-house grid (rooms endpoint absent / offline)
            int l1 = 0, l2 = 0;
            char *b1 = ui_http_get(s_url, &l1);
            char *b2 = ui_http_get(s_disp_url, &l2);
            cJSON *r1 = (b1 && l1 > 0) ? cJSON_Parse(b1) : NULL;
            cJSON *r2 = (b2 && l2 > 0) ? cJSON_Parse(b2) : NULL;
            cJSON *sensors = r1 ? cJSON_GetObjectItem(r1, "sensors") : NULL;
            cJSON *devices = r2 ? cJSON_GetObjectItem(r2, "devices") : NULL;
            cJSON *catalog = r1 ? cJSON_GetObjectItem(r1, "metrics") : NULL;
            if (cJSON_IsArray(sensors) || cJSON_IsArray(devices)) {
                if (lvgl_port_lock(0)) {
                    lv_obj_clear_flag(s_grid, LV_OBJ_FLAG_HIDDEN);
                    lv_obj_add_flag(s_map, LV_OBJ_FLAG_HIDDEN);
                    lv_obj_add_flag(s_back, LV_OBJ_FLAG_HIDDEN);
                    lvgl_port_unlock();
                }
                render(cJSON_IsArray(sensors) ? sensors : NULL, cJSON_IsArray(devices) ? devices : NULL,
                       catalog, NULL, "House");
            } else {
                ESP_LOGW(TAG, "fetch/parse failed");
                if (lvgl_port_lock(0)) { lv_label_set_text(s_header, "House  -  (offline)"); lvgl_port_unlock(); }
            }
            if (r1) cJSON_Delete(r1);
            if (r2) cJSON_Delete(r2);
            if (b1) heap_caps_free(b1);
            if (b2) heap_caps_free(b2);
        }
    }

    // ── whole-house scene state (top bar) — always ──
    int l3 = 0;
    char *b3 = ui_http_get(s_house_url, &l3);
    cJSON *r3 = (b3 && l3 > 0) ? cJSON_Parse(b3) : NULL;
    if (cJSON_IsObject(r3)) ui_scenes_render(r3);
    ui_admin_check_idle();       // drop the admin session after inactivity
    if (r3) cJSON_Delete(r3);
    if (b3) heap_caps_free(b3);
}

static void ui_task(void *pv)
{
    TickType_t last = xTaskGetTickCount() - pdMS_TO_TICKS(REFRESH_MS);   // render on first pass
    for (;;) {
        if (s_nav_dirty || (xTaskGetTickCount() - last) >= pdMS_TO_TICKS(REFRESH_MS)) {
            s_nav_dirty = false;
            last = xTaskGetTickCount();
            do_render_cycle();
        }
        vTaskDelay(pdMS_TO_TICKS(150));   // poll for nav changes (tap/back) for a snappy response
    }
}

// state_task just drains the MQTT-state queue into ui_grid_apply_state (LVGL off the mqtt stack).
static void state_task(void *pv)
{
    char *json;
    for (;;) {
        if (xQueueReceive(s_state_q, &json, portMAX_DELAY) == pdTRUE) {
            ui_grid_apply_state(json);
            free(json);
        }
    }
}

// Called from the MQTT callback: LIGHTWEIGHT — copy + enqueue only, no parse/LVGL.
void ui_tiles_set_battery(int pct, bool on_wall, bool gaining)
{
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    const char *batt = pct >= 88 ? LV_SYMBOL_BATTERY_FULL :
                       pct >= 63 ? LV_SYMBOL_BATTERY_3 :
                       pct >= 38 ? LV_SYMBOL_BATTERY_2 :
                       pct >= 13 ? LV_SYMBOL_BATTERY_1 : LV_SYMBOL_BATTERY_EMPTY;
    // Composite 3-state power glyph. Plug (USB) = external power present; bolt (CHARGE) = cell
    // voltage actually RISING (honest "charging", not just the STAT pin). So the operator tells
    // "on battery" from "plugged but holding" from "actively charging" at a glance (colour too).
    char txt[56]; uint32_t col;
    if (!on_wall) {                               // running on the cell
        snprintf(txt, sizeof(txt), "%s %d%%", batt, pct);
        col = (pct < 15) ? 0xf87171 : 0xcbd5e1;   // red when low, else slate
    } else if (gaining) {                         // wall power + cell voltage climbing
        snprintf(txt, sizeof(txt), "%s%s%s %d%%", LV_SYMBOL_USB, LV_SYMBOL_CHARGE, batt, pct);
        col = 0x22c55e;                           // green
    } else {                                      // wall power present, but holding (not gaining)
        snprintf(txt, sizeof(txt), "%s%s %d%%", LV_SYMBOL_USB, batt, pct);
        col = 0xfbbf24;                           // amber
    }
    if (lvgl_port_lock(0)) {
        if (s_batt_lbl) {
            lv_label_set_text(s_batt_lbl, txt);
            lv_obj_set_style_text_color(s_batt_lbl, lv_color_hex(col), 0);
        }
        lvgl_port_unlock();
    }
}

void ui_tiles_on_state(const char *json)
{
    if (!s_started || !s_state_q || !json) return;
    char *copy = strdup(json);
    if (!copy) return;
    if (xQueueSend(s_state_q, &copy, 0) != pdTRUE) free(copy);   // drop if backed up (stale anyway)
}

void ui_tiles_start(const char *sensors_url)
{
    strncpy(s_url, sensors_url, sizeof(s_url) - 1);
    snprintf(s_disp_url, sizeof(s_disp_url), "%s", s_url);      // sibling endpoint: /sensors -> /displays
    char *p = strstr(s_disp_url, "/sensors");
    if (p) strcpy(p, "/displays");
    char base[192];                                            // BFF base for /devices/<id>/command
    snprintf(base, sizeof(base), "%s", s_url);
    char *b = strstr(base, "/api/v1");
    if (b) *b = 0;
    ui_http_init(base);                                        // hand the base to the transport module
    snprintf(s_house_url, sizeof(s_house_url), "%s/api/v1/house", base);
    snprintf(s_map_url, sizeof(s_map_url), "%s/api/v1/rooms", base);
    s_started = true;

    if (!lvgl_port_lock(0)) { ESP_LOGE(TAG, "lvgl lock failed"); return; }
    lv_obj_t *scr = lv_scr_act();
    lv_obj_clean(scr);                        // drop the bring-up splash
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x0b1021), 0);
    lv_obj_set_style_pad_all(scr, 10, 0);
    lv_obj_set_flex_flow(scr, LV_FLEX_FLOW_COLUMN);

    // ── top bar: [ scene buttons ]  … toast …  [ battery ]  [ admin gear ] ──
    ui_scenes_init(scr);                      // top-bar row + scene box (ui/ui_scenes)
    lv_obj_t *topbar = ui_scenes_topbar();
    ui_admin_init(topbar);                    // shared toast line + auto-admin worker (ui/ui_admin; displays unlocked, no gear)

    s_batt_lbl = lv_label_create(topbar);     // battery indicator (MAX17048)
    lv_label_set_text(s_batt_lbl, "");
    lv_obj_set_style_text_font(s_batt_lbl, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(s_batt_lbl, lv_color_hex(0xcbd5e1), 0);
    lv_obj_set_style_pad_right(s_batt_lbl, 10, 0);
    // ui_admin appended [toast]; place the battery just after it (index 2 = after scenebox+toast),
    // keeping the scenebox/toast/battery/clock/power order.
    lv_obj_move_to_index(s_batt_lbl, 2);

    ui_clock_init(topbar);                    // wall clock (ui/ui_clock; roadmap #1, ability G)
    ui_power_init(topbar);                    // trailing red power-off button + confirm (ui/ui_power)

    // Title row: [ < back ]  [ title ].  The back button sits NEXT TO the view title (House / room
    // name) — deliberately NOT in the top-bar scene selector, so it never reads as a scene control.
    lv_obj_t *title_row = lv_obj_create(scr);
    lv_obj_set_width(title_row, lv_pct(100));
    lv_obj_set_height(title_row, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(title_row, 0, 0);
    lv_obj_set_style_border_width(title_row, 0, 0);
    lv_obj_set_style_pad_all(title_row, 0, 0);
    lv_obj_set_style_pad_bottom(title_row, 6, 0);
    lv_obj_set_style_pad_column(title_row, 12, 0);
    lv_obj_set_flex_flow(title_row, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(title_row, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(title_row, LV_OBJ_FLAG_SCROLLABLE);

    s_back = lv_obj_create(title_row);
    lv_obj_set_size(s_back, 128, 46);
    lv_obj_set_style_bg_color(s_back, lv_color_hex(0x16204a), 0);
    lv_obj_set_style_border_color(s_back, lv_color_hex(0x2f7e7a), 0);
    lv_obj_set_style_border_width(s_back, 2, 0);
    lv_obj_set_style_radius(s_back, 10, 0);
    lv_obj_set_style_pad_all(s_back, 0, 0);
    lv_obj_clear_flag(s_back, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_back, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(s_back, on_back_clicked, LV_EVENT_CLICKED, NULL);
    lv_obj_t *bklbl = lv_label_create(s_back);
    lv_label_set_text(bklbl, LV_SYMBOL_LEFT "  House");
    lv_obj_set_style_text_font(bklbl, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(bklbl, lv_color_hex(0xffffff), 0);
    lv_obj_center(bklbl);
    lv_obj_add_flag(s_back, LV_OBJ_FLAG_HIDDEN);   // shown only in a room

    s_header = lv_label_create(title_row);
    lv_label_set_text(s_header, "House");
    lv_obj_set_style_text_font(s_header, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(s_header, lv_color_hex(0xffffff), 0);

    // The screen itself scrolls vertically: [header] -> [tile grid] -> [expansion stack].
    lv_obj_set_scrollbar_mode(scr, LV_SCROLLBAR_MODE_AUTO);
    lv_obj_set_scroll_dir(scr, LV_DIR_VER);

    s_grid = lv_obj_create(scr);
    lv_obj_set_width(s_grid, lv_pct(100));
    lv_obj_set_height(s_grid, LV_SIZE_CONTENT);      // size to its rows so expansions can stack below
    lv_obj_set_style_bg_opa(s_grid, 0, 0);
    lv_obj_set_style_border_width(s_grid, 0, 0);
    lv_obj_set_style_pad_all(s_grid, 0, 0);
    lv_obj_set_style_pad_row(s_grid, 10, 0);
    lv_obj_set_style_pad_column(s_grid, 10, 0);
    lv_obj_set_flex_flow(s_grid, LV_FLEX_FLOW_ROW_WRAP);
    lv_obj_clear_flag(s_grid, LV_OBJ_FLAG_SCROLLABLE);   // the screen scrolls, not the grid

    // House-map container: fixed-height, absolute-positioned room chips (ui/ui_map). Shown as the
    // landing view once the first /api/v1/rooms lands; the grid above is the offline fallback.
    s_map = lv_obj_create(scr);
    lv_obj_set_width(s_map, lv_pct(100));
    lv_obj_set_height(s_map, 1040);
    lv_obj_set_style_bg_opa(s_map, 0, 0);
    lv_obj_set_style_border_width(s_map, 0, 0);
    lv_obj_set_style_pad_all(s_map, 0, 0);
    lv_obj_set_layout(s_map, LV_LAYOUT_NONE);
    lv_obj_clear_flag(s_map, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_map, LV_OBJ_FLAG_HIDDEN);

    ui_expand_init(scr);      // inline expansion stack below the grid (ui/ui_expand)
    ui_controls_init();       // actuator command overlay + worker (ui/ui_controls)

    lvgl_port_unlock();

    s_state_q = xQueueCreate(24, sizeof(char *));   // MQTT state payloads
    xTaskCreate(state_task, "uistate", 6144, NULL, 4, NULL);
    xTaskCreate(ui_task, "ui", 8192, NULL, 4, NULL);
    ui_chart_start();   // expansion chart fetch worker + queue (ui/ui_chart)
    ESP_LOGI(TAG, "ui_tiles started -> %s", s_url);
}
