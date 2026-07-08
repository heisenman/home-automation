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
#include "esp_timer.h"     // esp_timer_get_time() for the time-boxed catalog re-prime
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
#include "ui/ui_devices.h"  // devices-management screen: list -> reassign room (canonical relocate)
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
static int64_t s_catalog_primed_ms;  // last time the shared catalog was (re)primed from /api/v1/sensors
                               // (0 = never). /api/v1/rooms carries no metric catalog, so the landing map
                               // renders off this primed copy; re-priming periodically (not once-per-boot)
                               // lets server catalog changes — new metrics / units like AQ, VOC — reach the
                               // map without a panel reboot.
#define CATALOG_REPRIME_MS (10 * 60 * 1000)  // re-prime cadence: one cheap /api/v1/sensors GET / 10 min
static lv_obj_t *s_header, *s_grid;
static lv_obj_t *s_map;       // house-map container (absolute-positioned room chips)
static lv_obj_t *s_devices;   // devices-management screen container (list -> reassign room)
static lv_obj_t *s_back;      // "< Rooms" back button (shown only in the room-zoom / devices view)
static lv_obj_t *s_devbtn;    // "Devices" entry button in the title row (shown only on the house map)
static lv_obj_t *s_batt_lbl;  // battery indicator in the top bar

static char s_room[28];            // selected room slug for room-zoom; "" = house map (landing)
static char s_room_name[28];       // its display name (for the title label)
static volatile bool s_devview;    // devices-management screen active (overrides map/room)
static volatile bool s_roomspatial;// room-zoom shows the spatial diagram (arc 3); a device tap -> grid
static volatile bool s_nav_dirty;  // set on a nav change -> ui_task re-renders promptly (no 10s wait)

// House-map room tap -> enter that room's zoom view (the device grid filtered to this room).
static void on_room_tap(const char *area_id, const char *name)
{
    if (!area_id) return;
    strncpy(s_room, area_id, sizeof s_room - 1);
    s_room[sizeof s_room - 1] = 0;
    strncpy(s_room_name, (name && name[0]) ? name : area_id, sizeof s_room_name - 1);
    s_room_name[sizeof s_room_name - 1] = 0;
    s_roomspatial = true;        // enter the room on its spatial diagram (falls to grid if no geometry)
    ui_expand_clear();           // nav change -> drop any inline charts (LVGL lock held in this cb)
    s_nav_dirty = true;
    ESP_LOGI(TAG, "nav -> room %s", s_room);
}

// Spatial room-zoom device tap -> drop to that room's tile grid, where the full inline detail
// (expand + 72h chart + controls) lives. The panel stays read-only; this is just navigation.
static void on_room_device_tap(const char *device_id, const char *name)
{
    (void)name;
    ESP_LOGI(TAG, "room-device tap %s -> grid detail", device_id ? device_id : "?");
    s_roomspatial = false;
    s_nav_dirty = true;
}

// Back button (room-zoom OR devices view) -> return to the house map.
static void on_back_clicked(lv_event_t *e)
{
    (void)e;
    s_room[0] = 0;
    s_devview = false;
    ui_expand_clear();           // leaving the room -> destroy its inline charts (not persisted)
    s_nav_dirty = true;
}

// "Devices" button on the house map -> open the devices-management screen.
static void on_devices_clicked(lv_event_t *e)
{
    (void)e;
    s_devview = true;
    s_nav_dirty = true;
}

// A devices-screen row was confirmed -> turn the staged fields into the right writes, through the admin
// worker (HTTP off the click stack; auto-JWT + 401 retry). Location = canonical relocate (restamp:
// correction phase); Name + Status = the display overlay (one PUT /meta). Unstaged fields arrive empty.
static void on_devices_commit(const char *device_id, const char *new_room, const char *new_room_name,
                              const char *new_name, int new_status)
{
    if (!device_id) return;
    if (new_room && new_room[0]) {
        char body[160];
        snprintf(body, sizeof body, "{\"new_area\":\"%s\",\"mode\":\"restamp\",\"dry_run\":false}", new_room);
        ui_admin_set_relocate(device_id, body);
    }
    if ((new_name && new_name[0]) || new_status >= 0) {
        char body[240];
        int n = snprintf(body, sizeof body, "{");
        bool first = true;
        if (new_name && new_name[0]) {
            n += snprintf(body + n, sizeof body - n, "\"name\":\"%s\"", new_name);
            first = false;
        }
        if (new_status >= 0) {                                  // Active=neither, Hidden=hidden, Retired=retired
            n += snprintf(body + n, sizeof body - n, "%s\"hidden\":%s,\"retired\":%s",
                          first ? "" : ",",
                          new_status == UI_DEV_HIDDEN ? "true" : "false",
                          new_status == UI_DEV_RETIRED ? "true" : "false");
        }
        snprintf(body + n, sizeof body - n, "}");
        ui_admin_set_meta(device_id, body);
    }
    ui_toast("saving...");
    (void)new_room_name;
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
// `nav` = this cycle was triggered by a nav change (entry/back), not the periodic timer. The devices
// screen is management data that changes rarely, so it renders only on `nav` and stays static otherwise —
// no 10s full-table teardown, no flicker. The map/room views still refresh live sensor values each tick.
static void do_render_cycle(bool nav)
{
    // (Re)prime the shared metric catalog periodically (the map's /api/v1/rooms doesn't carry it, so
    // without this the landing view renders gas/index metrics on the temp/hum fallback — and server-added
    // metrics/units would need a reboot). Time-boxed re-prime keeps it fresh at negligible cost.
    int64_t now_ms = esp_timer_get_time() / 1000;
    if (s_catalog_primed_ms == 0 || now_ms - s_catalog_primed_ms >= CATALOG_REPRIME_MS) {
        int lc = 0;
        char *bc = ui_http_get(s_url, &lc);          // /api/v1/sensors carries the `metrics` catalog
        if (bc && lc > 0) {
            cJSON *rc = cJSON_Parse(bc);
            cJSON *cat = rc ? cJSON_GetObjectItem(rc, "metrics") : NULL;
            if (cJSON_IsArray(cat) && cJSON_GetArraySize(cat) > 0 && lvgl_port_lock(0)) {
                ui_format_load_catalog(cat);
                lvgl_port_unlock();
                s_catalog_primed_ms = now_ms;
            }
            if (rc) cJSON_Delete(rc);
        }
        if (bc) heap_caps_free(bc);
    }

    if (s_devview) {
      if (nav) {                                         // render only on entry — static after, no flicker
        // ── devices-management screen: editable Device/Location/Status table ──
        int lm = 0, lmeta = 0;
        char *bm = ui_http_get(s_map_url, &lm);          // /api/v1/rooms (device list + room options)
        char meta_url[192];
        snprintf(meta_url, sizeof meta_url, "%s/api/v1/devices/meta", ui_http_base());
        char *bmeta = ui_http_get(meta_url, &lmeta);     // /api/v1/devices/meta (name/hidden/retired overlay)
        cJSON *rm = (bm && lm > 0) ? cJSON_Parse(bm) : NULL;
        cJSON *rmeta = (bmeta && lmeta > 0) ? cJSON_Parse(bmeta) : NULL;
        if (lvgl_port_lock(0)) {
            ui_devices_render(rm, rmeta, s_devices, on_devices_commit);
            lv_obj_clear_flag(s_devices, LV_OBJ_FLAG_HIDDEN);
            lv_obj_add_flag(s_map, LV_OBJ_FLAG_HIDDEN);
            lv_obj_add_flag(s_grid, LV_OBJ_FLAG_HIDDEN);
            lv_obj_add_flag(s_devbtn, LV_OBJ_FLAG_HIDDEN);
            lv_obj_clear_flag(s_back, LV_OBJ_FLAG_HIDDEN);
            lv_label_set_text(s_header, "Devices");
            lvgl_port_unlock();
        }
        if (rm) cJSON_Delete(rm);
        if (rmeta) cJSON_Delete(rmeta);
        if (bm) heap_caps_free(bm);
        if (bmeta) heap_caps_free(bmeta);
      }
    } else if (s_room[0] && s_roomspatial) {
        // ── room-zoom (arc 3): spatial diagram — placed devices on the room polygon ──
        int lm = 0;
        char *bm = ui_http_get(s_map_url, &lm);          // /api/v1/rooms (geometry + devices + placement)
        cJSON *rm = (bm && lm > 0) ? cJSON_Parse(bm) : NULL;
        bool ok = false;
        if (rm && lvgl_port_lock(0)) {
            ok = ui_map_render_room(rm, s_room, s_map, on_room_device_tap);
            if (ok) {
                lv_obj_clear_flag(s_map, LV_OBJ_FLAG_HIDDEN);
                lv_obj_add_flag(s_grid, LV_OBJ_FLAG_HIDDEN);
                lv_obj_add_flag(s_devices, LV_OBJ_FLAG_HIDDEN);
                lv_obj_add_flag(s_devbtn, LV_OBJ_FLAG_HIDDEN);
                lv_obj_clear_flag(s_back, LV_OBJ_FLAG_HIDDEN);
                lv_label_set_text(s_header, s_room_name);
            }
            lvgl_port_unlock();
        }
        if (rm) cJSON_Delete(rm);
        if (bm) heap_caps_free(bm);
        if (!ok) { s_roomspatial = false; s_nav_dirty = true; }   // no geometry -> tile grid next cycle
    } else if (s_room[0]) {
        // ── room-zoom: the device grid filtered to the selected room (detail lives here) ──
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
            lv_obj_add_flag(s_devices, LV_OBJ_FLAG_HIDDEN);
            lv_obj_add_flag(s_devbtn, LV_OBJ_FLAG_HIDDEN);
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
                lv_obj_add_flag(s_devices, LV_OBJ_FLAG_HIDDEN);
                lv_obj_add_flag(s_back, LV_OBJ_FLAG_HIDDEN);
                lv_obj_clear_flag(s_devbtn, LV_OBJ_FLAG_HIDDEN);   // Devices entry only on the map
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
                    lv_obj_add_flag(s_devices, LV_OBJ_FLAG_HIDDEN);
                    lv_obj_add_flag(s_devbtn, LV_OBJ_FLAG_HIDDEN);   // no rooms data -> no devices screen
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
        bool nav = s_nav_dirty;
        if (nav || (xTaskGetTickCount() - last) >= pdMS_TO_TICKS(REFRESH_MS)) {
            s_nav_dirty = false;
            last = xTaskGetTickCount();
            do_render_cycle(nav);
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
    lv_obj_set_flex_grow(s_header, 1);      // push the Devices button to the right edge of the title row

    // "Devices" entry -> the devices-management screen. Shown only on the house map (hidden in
    // room-zoom / devices views, where the back button takes the title-row slot instead).
    s_devbtn = lv_obj_create(title_row);
    lv_obj_set_size(s_devbtn, 156, 46);
    lv_obj_set_style_bg_color(s_devbtn, lv_color_hex(0x16204a), 0);
    lv_obj_set_style_border_color(s_devbtn, lv_color_hex(0x2f7e7a), 0);
    lv_obj_set_style_border_width(s_devbtn, 2, 0);
    lv_obj_set_style_radius(s_devbtn, 10, 0);
    lv_obj_set_style_pad_all(s_devbtn, 0, 0);
    lv_obj_clear_flag(s_devbtn, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_devbtn, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(s_devbtn, on_devices_clicked, LV_EVENT_CLICKED, NULL);
    lv_obj_t *dblbl = lv_label_create(s_devbtn);
    lv_label_set_text(dblbl, LV_SYMBOL_LIST "  Devices");
    lv_obj_set_style_text_font(dblbl, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(dblbl, lv_color_hex(0xffffff), 0);
    lv_obj_center(dblbl);

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

    // House-map container: fills all remaining height below the title (flex_grow) so the floor plan
    // scales to the bottom of the screen (the scr has a 10px inset = ~10px off the edge). Absolute-
    // positioned room labels + wall lines (ui/ui_map). Landing view once the first /api/v1/rooms lands.
    s_map = lv_obj_create(scr);
    lv_obj_set_width(s_map, lv_pct(100));
    lv_obj_set_height(s_map, 0);
    lv_obj_set_flex_grow(s_map, 1);            // take all leftover vertical space
    lv_obj_set_style_bg_opa(s_map, 0, 0);
    lv_obj_set_style_border_width(s_map, 0, 0);
    lv_obj_set_style_pad_all(s_map, 0, 0);
    lv_obj_set_layout(s_map, LV_LAYOUT_NONE);
    lv_obj_clear_flag(s_map, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_map, LV_OBJ_FLAG_HIDDEN);

    // Devices-management screen: a scrollable vertical list (device rows grouped by room). Fills the
    // leftover height like the map (only one of the two is ever visible); its own content scrolls.
    s_devices = lv_obj_create(scr);
    lv_obj_set_width(s_devices, lv_pct(100));
    lv_obj_set_height(s_devices, 0);
    lv_obj_set_flex_grow(s_devices, 1);
    lv_obj_set_style_bg_opa(s_devices, 0, 0);
    lv_obj_set_style_border_width(s_devices, 0, 0);
    lv_obj_set_style_pad_all(s_devices, 0, 0);
    lv_obj_set_style_pad_row(s_devices, 6, 0);
    lv_obj_set_flex_flow(s_devices, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_scroll_dir(s_devices, LV_DIR_VER);
    lv_obj_add_flag(s_devices, LV_OBJ_FLAG_HIDDEN);

    ui_expand_init(scr);      // inline expansion stack below the grid (ui/ui_expand)
    ui_controls_init();       // actuator command overlay + worker (ui/ui_controls)

    lvgl_port_unlock();

    s_state_q = xQueueCreate(24, sizeof(char *));   // MQTT state payloads
    xTaskCreate(state_task, "uistate", 6144, NULL, 4, NULL);
    xTaskCreate(ui_task, "ui", 8192, NULL, 4, NULL);
    ui_chart_start();   // expansion chart fetch worker + queue (ui/ui_chart)
    ESP_LOGI(TAG, "ui_tiles started -> %s", s_url);
}
