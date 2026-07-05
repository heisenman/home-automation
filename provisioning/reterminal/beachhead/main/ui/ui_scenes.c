// Top bar + scene selector (see ui_scenes.h). Extracted from ui_tiles.c verbatim (ADR-0020
// module-first); behavior identical. scene_btn_cb now checks ui_admin_active() and enqueues via
// ui_admin_set_scene() + reports via ui_toast(), instead of touching the admin queue/toast directly.
#include "ui/ui_scenes.h"
#include "ui/ui_admin.h"    // active / toast / set_scene
#include <string.h>
#include <stdio.h>
#include "lvgl.h"
#include "esp_lvgl_port.h"
#include "cJSON.h"

// ── top bar: scene selector (Home/Away/Sleep) (ADR-0019 Phase B) ──
static lv_obj_t *s_topbar, *s_scenebox;
#define MAX_SCENES 4
static lv_obj_t *s_scene_btn[MAX_SCENES];
static char s_scene_name[MAX_SCENES][16];
static int s_nscenes;
static char s_scene_active[16];    // currently-active scene (from /api/v1/house)
static char s_scene_notified[16];  // last scene the on-change hook fired for
static void (*s_on_change)(const char *scene);   // device-local scene->backlight hook (composition root)

lv_obj_t *ui_scenes_topbar(void) { return s_topbar; }

void ui_scenes_set_on_change(void (*cb)(const char *scene)) { s_on_change = cb; }

bool ui_scenes_active_scene(char *out, size_t len)
{
    if (!out || len == 0) return false;
    snprintf(out, len, "%s", s_scene_active);
    return s_scene_active[0] != 0;
}

// scene button: request the scene (admin only). Runs in LVGL/click ctx — enqueue only.
static void scene_btn_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx < 0 || idx >= s_nscenes) return;
    if (!ui_admin_active()) { ui_toast("unlock (gear) to change scene"); return; }
    ui_admin_set_scene(s_scene_name[idx]);
    ui_toast("setting scene…");
}

// Build the scene buttons once (spec-driven from /api/v1/house `scenes`) + refresh the active
// highlight each fetch. Called from ui_task; takes the LVGL lock itself.
void ui_scenes_render(cJSON *house)
{
    cJSON *scene = cJSON_GetObjectItem(house, "scene");
    cJSON *scenes = cJSON_GetObjectItem(house, "scenes");
    if (!lvgl_port_lock(0)) return;
    if (cJSON_IsString(scene)) snprintf(s_scene_active, sizeof(s_scene_active), "%s", scene->valuestring);
    if (s_nscenes == 0 && cJSON_IsArray(scenes) && s_scenebox) {
        cJSON *sc;
        cJSON_ArrayForEach(sc, scenes) {
            if (s_nscenes >= MAX_SCENES || !cJSON_IsString(sc)) break;
            int i = s_nscenes;
            snprintf(s_scene_name[i], sizeof(s_scene_name[i]), "%s", sc->valuestring);
            lv_obj_t *b = lv_button_create(s_scenebox);
            lv_obj_set_height(b, 46);
            lv_obj_add_event_cb(b, scene_btn_cb, LV_EVENT_CLICKED, (void *)(intptr_t)i);
            lv_obj_t *l = lv_label_create(b);
            lv_label_set_text(l, s_scene_name[i]);
            lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0);
            lv_obj_center(l);
            s_scene_btn[i] = b;
            s_nscenes++;
        }
    }
    for (int i = 0; i < s_nscenes; i++)          // highlight the active scene
        lv_obj_set_style_bg_color(s_scene_btn[i],
            lv_color_hex(strcmp(s_scene_name[i], s_scene_active) == 0 ? 0x2563eb : 0x1e293b), 0);
    // Detect an active-scene change under the lock, but fire the hook AFTER unlocking so the
    // callback may drive bsp_display_* / MQTT without nesting the LVGL lock.
    bool changed = s_on_change && s_scene_active[0] && strcmp(s_scene_active, s_scene_notified) != 0;
    char newscene[16];
    if (changed) { snprintf(newscene, sizeof(newscene), "%s", s_scene_active);
                   snprintf(s_scene_notified, sizeof(s_scene_notified), "%s", s_scene_active); }
    lvgl_port_unlock();
    if (changed && s_on_change) s_on_change(newscene);
}

void ui_scenes_init(lv_obj_t *scr)
{
    // ── top bar: [ scene buttons ]  … toast …  [ battery ]  [ admin gear ] ──
    s_topbar = lv_obj_create(scr);
    lv_obj_set_width(s_topbar, lv_pct(100));
    lv_obj_set_height(s_topbar, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(s_topbar, 0, 0);
    lv_obj_set_style_border_width(s_topbar, 0, 0);
    lv_obj_set_style_pad_all(s_topbar, 0, 0);
    lv_obj_set_style_pad_bottom(s_topbar, 8, 0);
    lv_obj_set_flex_flow(s_topbar, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(s_topbar, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(s_topbar, LV_OBJ_FLAG_SCROLLABLE);

    s_scenebox = lv_obj_create(s_topbar);          // scene buttons (filled by ui_scenes_render)
    lv_obj_set_height(s_scenebox, LV_SIZE_CONTENT);
    lv_obj_set_flex_grow(s_scenebox, 1);
    lv_obj_set_style_bg_opa(s_scenebox, 0, 0);
    lv_obj_set_style_border_width(s_scenebox, 0, 0);
    lv_obj_set_style_pad_all(s_scenebox, 0, 0);
    lv_obj_set_style_pad_column(s_scenebox, 8, 0);
    lv_obj_set_flex_flow(s_scenebox, LV_FLEX_FLOW_ROW);
    lv_obj_clear_flag(s_scenebox, LV_OBJ_FLAG_SCROLLABLE);
}
