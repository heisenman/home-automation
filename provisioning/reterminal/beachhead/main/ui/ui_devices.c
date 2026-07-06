// Devices-management screen (see ui_devices.h). Lists devices grouped by their current room; tapping a
// device opens a modal room-picker; picking a room fires the relocate callback. Pure LVGL widgets +
// labels (no lv_canvas) — same PSRAM-safe infra as ui_map. Runs under the LVGL lock (render) / in the
// click context (picker). Fonts: only montserrat 14/20/28 are compiled in.
#include "ui/ui_devices.h"
#include <string.h>
#include <stdint.h>
#include <stdio.h>
#include "lvgl.h"

#define MAX_DEV  64
#define MAX_AREA 40

struct devrow  { char id[40]; char name[40]; char room_id[28]; char room_name[28]; };
struct arearow { char id[28]; char name[28]; };

static struct devrow  s_dev[MAX_DEV];
static struct arearow s_area[MAX_AREA];
static int s_ndev, s_narea;
static ui_devices_relocate_cb s_cb;

static lv_obj_t *s_picker;                    // modal overlay on lv_layer_top(); NULL when closed
static char s_pick_id[40], s_pick_name[40];   // the device being moved (captured at open)

static void picker_close(void)
{
    if (s_picker) { lv_obj_del(s_picker); s_picker = NULL; }
}

// A room button in the picker was tapped -> relocate the captured device there, close the modal.
static void picker_area_cb(lv_event_t *e)
{
    int i = (int)(intptr_t)lv_event_get_user_data(e);
    if (i >= 0 && i < s_narea && s_cb) s_cb(s_pick_id, s_area[i].id, s_area[i].name);
    picker_close();
}

static void picker_cancel_cb(lv_event_t *e) { (void)e; picker_close(); }

// Build the modal room-picker for device row `dev_idx`: a titled, scrollable list of every room.
static void open_picker(int dev_idx)
{
    if (dev_idx < 0 || dev_idx >= s_ndev) return;
    snprintf(s_pick_id, sizeof s_pick_id, "%s", s_dev[dev_idx].id);
    snprintf(s_pick_name, sizeof s_pick_name, "%s", s_dev[dev_idx].name);
    picker_close();

    lv_obj_t *ov = lv_obj_create(lv_layer_top());          // full-screen dim, intercepts stray taps
    s_picker = ov;
    lv_obj_set_size(ov, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(ov, lv_color_hex(0x000000), 0);
    lv_obj_set_style_bg_opa(ov, LV_OPA_70, 0);
    lv_obj_set_style_border_width(ov, 0, 0);
    lv_obj_set_style_pad_all(ov, 0, 0);
    lv_obj_clear_flag(ov, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *card = lv_obj_create(ov);
    lv_obj_set_size(card, lv_pct(86), lv_pct(82));
    lv_obj_center(card);
    lv_obj_set_style_bg_color(card, lv_color_hex(0x111834), 0);
    lv_obj_set_style_border_width(card, 0, 0);
    lv_obj_set_style_radius(card, 12, 0);
    lv_obj_set_style_pad_all(card, 14, 0);
    lv_obj_set_style_pad_row(card, 8, 0);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *ttl = lv_label_create(card);
    lv_label_set_text_fmt(ttl, "Move %s to:", s_pick_name);
    lv_label_set_long_mode(ttl, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(ttl, lv_pct(100));
    lv_obj_set_style_text_font(ttl, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(ttl, lv_color_hex(0xffffff), 0);

    lv_obj_t *note = lv_label_create(card);       // restamp is NOT silent (correction phase)
    lv_label_set_text(note, "Reassign corrects the record (moves this device's history too).");
    lv_label_set_long_mode(note, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(note, lv_pct(100));
    lv_obj_set_style_text_font(note, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(note, lv_color_hex(0x8fb4ff), 0);

    lv_obj_t *list = lv_obj_create(card);         // the scrollable room list takes the leftover height
    lv_obj_set_width(list, lv_pct(100));
    lv_obj_set_flex_grow(list, 1);
    lv_obj_set_style_bg_opa(list, 0, 0);
    lv_obj_set_style_border_width(list, 0, 0);
    lv_obj_set_style_pad_all(list, 0, 0);
    lv_obj_set_style_pad_row(list, 6, 0);
    lv_obj_set_flex_flow(list, LV_FLEX_FLOW_COLUMN);
    for (int i = 0; i < s_narea; i++) {
        lv_obj_t *b = lv_button_create(list);
        lv_obj_set_width(b, lv_pct(100));
        lv_obj_set_style_bg_color(b, lv_color_hex(0x16204a), 0);
        lv_obj_add_event_cb(b, picker_area_cb, LV_EVENT_CLICKED, (void *)(intptr_t)i);
        lv_obj_t *l = lv_label_create(b);
        lv_label_set_text(l, s_area[i].name);
        lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0);
        lv_obj_center(l);
    }

    lv_obj_t *cancel = lv_button_create(card);
    lv_obj_set_width(cancel, lv_pct(100));
    lv_obj_set_style_bg_color(cancel, lv_color_hex(0x334155), 0);
    lv_obj_add_event_cb(cancel, picker_cancel_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *cl = lv_label_create(cancel);
    lv_label_set_text(cl, "Cancel");
    lv_obj_set_style_text_font(cl, &lv_font_montserrat_20, 0);
    lv_obj_center(cl);
}

static void devrow_cb(lv_event_t *e)
{
    open_picker((int)(intptr_t)lv_event_get_user_data(e));
}

void ui_devices_render(cJSON *root, lv_obj_t *parent, ui_devices_relocate_cb cb)
{
    if (s_picker) return;              // a pick is in progress — don't rebuild underneath the modal
    s_cb = cb;
    s_ndev = 0; s_narea = 0;
    lv_obj_clean(parent);

    cJSON *rooms = root ? cJSON_GetObjectItem(root, "rooms") : NULL;
    if (!cJSON_IsArray(rooms)) {
        lv_obj_t *l = lv_label_create(parent);
        lv_label_set_text(l, "(devices unavailable)");
        lv_obj_set_style_text_color(l, lv_color_hex(0x64748b), 0);
        return;
    }

    // pass 1: every room -> a picker option (even empty rooms are valid move targets)
    cJSON *room;
    cJSON_ArrayForEach(room, rooms) {
        if (s_narea >= MAX_AREA) break;
        cJSON *id = cJSON_GetObjectItem(room, "id");
        cJSON *nm = cJSON_GetObjectItem(room, "name");
        if (!cJSON_IsString(id)) continue;
        snprintf(s_area[s_narea].id, sizeof s_area[0].id, "%s", id->valuestring);
        snprintf(s_area[s_narea].name, sizeof s_area[0].name, "%s",
                 cJSON_IsString(nm) ? nm->valuestring : id->valuestring);
        s_narea++;
    }

    // pass 2: the list — a room header, then each of its devices as a tappable row
    cJSON_ArrayForEach(room, rooms) {
        cJSON *devs = cJSON_GetObjectItem(room, "devices");
        if (!cJSON_IsArray(devs) || cJSON_GetArraySize(devs) == 0) continue;
        cJSON *rid = cJSON_GetObjectItem(room, "id");
        cJSON *rnm = cJSON_GetObjectItem(room, "name");
        const char *room_id = cJSON_IsString(rid) ? rid->valuestring : "";
        const char *room_nm = cJSON_IsString(rnm) ? rnm->valuestring : room_id;

        lv_obj_t *h = lv_label_create(parent);
        lv_label_set_text(h, room_nm);
        lv_obj_set_style_text_font(h, &lv_font_montserrat_20, 0);
        lv_obj_set_style_text_color(h, lv_color_hex(0x8fb4ff), 0);
        lv_obj_set_style_pad_top(h, 8, 0);

        cJSON *d;
        cJSON_ArrayForEach(d, devs) {
            if (s_ndev >= MAX_DEV) break;
            cJSON *did = cJSON_GetObjectItem(d, "device_id");
            cJSON *dnm = cJSON_GetObjectItem(d, "name");
            if (!cJSON_IsString(did)) continue;
            struct devrow *dr = &s_dev[s_ndev];
            snprintf(dr->id, sizeof dr->id, "%s", did->valuestring);
            snprintf(dr->name, sizeof dr->name, "%s", cJSON_IsString(dnm) ? dnm->valuestring : did->valuestring);
            snprintf(dr->room_id, sizeof dr->room_id, "%s", room_id);
            snprintf(dr->room_name, sizeof dr->room_name, "%s", room_nm);

            lv_obj_t *b = lv_button_create(parent);
            lv_obj_set_width(b, lv_pct(100));
            lv_obj_set_style_bg_color(b, lv_color_hex(0x16204a), 0);
            lv_obj_set_style_pad_all(b, 10, 0);
            lv_obj_add_event_cb(b, devrow_cb, LV_EVENT_CLICKED, (void *)(intptr_t)s_ndev);
            lv_obj_t *l = lv_label_create(b);
            lv_label_set_text(l, dr->name);
            lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0);
            lv_obj_set_style_text_color(l, lv_color_hex(0xffffff), 0);
            s_ndev++;
        }
    }

    if (s_ndev == 0) {
        lv_obj_t *l = lv_label_create(parent);
        lv_label_set_text(l, "(no devices)");
        lv_obj_set_style_text_color(l, lv_color_hex(0x64748b), 0);
    }
}
