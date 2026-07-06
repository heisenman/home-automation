// Devices-management screen (see ui_devices.h). An editable table: Device | Location | Status | (confirm).
// Editing a cell STAGES a change (cell border turns amber, the row's confirm lights up); tapping confirm
// commits via the callback. A background refresh is skipped while a modal is open or any edit is staged, so
// in-progress work is never yanked away. Pure LVGL widgets + the built-in keyboard (no lv_canvas).
#include "ui/ui_devices.h"
#include <string.h>
#include <stdint.h>
#include <stdio.h>
#include "lvgl.h"

#define MAX_DEV  64
#define MAX_AREA 40

struct devrow {
    char id[40], name[40], room_id[28], room_name[28];
    int status;                                   // current UI_DEV_*
    bool st_name, st_loc, st_status;              // staged?
    char s_name[40], s_room[28], s_room_name[28];
    int s_status;
    lv_obj_t *name_btn, *loc_btn, *stat_btn, *confirm;   // cell widgets (to restyle on stage)
    lv_obj_t *name_lbl, *loc_lbl, *stat_lbl;
};
struct arearow { char id[28]; char name[28]; };

static struct devrow  s_dev[MAX_DEV];
static struct arearow s_area[MAX_AREA];
static int s_ndev, s_narea;
static ui_devices_commit_cb s_cb;

static lv_obj_t *s_modal;          // active editor overlay (lv_layer_top); NULL when none
static int s_edit = -1;            // row index being edited (for the modals)

#define C_STAGE   0xf59e0b         // amber: a staged (uncommitted) cell
#define C_CELL    0x16204a
#define C_OK_ON   0x16a34a
#define C_OK_OFF  0x334155

static const char *status_text(int s){ return s==UI_DEV_HIDDEN?"Hidden":s==UI_DEV_RETIRED?"Retired":"Active"; }
static uint32_t status_color(int s){ return s==UI_DEV_HIDDEN?0x94a3b8:s==UI_DEV_RETIRED?0xef4444:0x22c55e; }

static bool any_staged(void)
{
    for (int i=0;i<s_ndev;i++) if (s_dev[i].st_name||s_dev[i].st_loc||s_dev[i].st_status) return true;
    return false;
}

static int find_dev(const char *id)
{
    for (int i=0;i<s_ndev;i++) if (strcmp(s_dev[i].id,id)==0) return i;
    return -1;
}

static void row_refresh_confirm(struct devrow *d)
{
    bool on = d->st_name||d->st_loc||d->st_status;
    lv_obj_set_style_bg_color(d->confirm, lv_color_hex(on?C_OK_ON:C_OK_OFF), 0);
    if (on) lv_obj_add_flag(d->confirm, LV_OBJ_FLAG_CLICKABLE);
    else    lv_obj_clear_flag(d->confirm, LV_OBJ_FLAG_CLICKABLE);
}

static void mark_staged(lv_obj_t *cell){ lv_obj_set_style_border_color(cell, lv_color_hex(C_STAGE),0);
                                         lv_obj_set_style_border_width(cell, 3, 0); }

static void modal_close(void){ if (s_modal){ lv_obj_del(s_modal); s_modal=NULL; } s_edit=-1; }

// ── staging (called from the editor modals) ─────────────────────────────────────────────────────────
static void stage_name(struct devrow *d, const char *name)
{
    snprintf(d->s_name, sizeof d->s_name, "%s", name);
    d->st_name = true;
    lv_label_set_text(d->name_lbl, d->s_name);
    mark_staged(d->name_btn);
    row_refresh_confirm(d);
}
static void stage_loc(struct devrow *d, const char *rid, const char *rname)
{
    snprintf(d->s_room, sizeof d->s_room, "%s", rid);
    snprintf(d->s_room_name, sizeof d->s_room_name, "%s", rname);
    d->st_loc = true;
    lv_label_set_text(d->loc_lbl, d->s_room_name);
    mark_staged(d->loc_btn);
    row_refresh_confirm(d);
}
static void stage_status(struct devrow *d, int st)
{
    d->s_status = st;
    d->st_status = true;
    lv_label_set_text(d->stat_lbl, status_text(st));
    lv_obj_set_style_text_color(d->stat_lbl, lv_color_hex(status_color(st)), 0);
    mark_staged(d->stat_btn);
    row_refresh_confirm(d);
}

// ── modal: a centered card on the dimmed top layer ──────────────────────────────────────────────────
static lv_obj_t *modal_card(int wpct, int hpct)
{
    lv_obj_t *ov = lv_obj_create(lv_layer_top());
    s_modal = ov;
    lv_obj_set_size(ov, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(ov, lv_color_hex(0x000000), 0);
    lv_obj_set_style_bg_opa(ov, LV_OPA_70, 0);
    lv_obj_set_style_border_width(ov, 0, 0);
    lv_obj_set_style_pad_all(ov, 0, 0);
    lv_obj_clear_flag(ov, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_t *card = lv_obj_create(ov);
    lv_obj_set_size(card, lv_pct(wpct), lv_pct(hpct));
    lv_obj_center(card);
    lv_obj_set_style_bg_color(card, lv_color_hex(0x111834), 0);
    lv_obj_set_style_border_width(card, 0, 0);
    lv_obj_set_style_radius(card, 12, 0);
    lv_obj_set_style_pad_all(card, 14, 0);
    lv_obj_set_style_pad_row(card, 8, 0);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);
    return card;
}

static lv_obj_t *card_title(lv_obj_t *card, const char *txt)
{
    lv_obj_t *t = lv_label_create(card);
    lv_label_set_text(t, txt);
    lv_label_set_long_mode(t, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(t, lv_pct(100));
    lv_obj_set_style_text_font(t, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(t, lv_color_hex(0xffffff), 0);
    return t;
}

static lv_obj_t *card_cancel(lv_obj_t *card)
{
    lv_obj_t *b = lv_button_create(card);
    lv_obj_set_width(b, lv_pct(100));
    lv_obj_set_style_bg_color(b, lv_color_hex(0x334155), 0);
    lv_obj_t *l = lv_label_create(b); lv_label_set_text(l, "Cancel");
    lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0); lv_obj_center(l);
    return b;
}
static void cancel_cb(lv_event_t *e){ (void)e; modal_close(); }

// ── Location editor: pick a room ────────────────────────────────────────────────────────────────────
static void loc_pick_cb(lv_event_t *e)
{
    int i = (int)(intptr_t)lv_event_get_user_data(e);
    if (s_edit>=0 && s_edit<s_ndev && i>=0 && i<s_narea) stage_loc(&s_dev[s_edit], s_area[i].id, s_area[i].name);
    modal_close();
}
static void open_loc_picker(int row)
{
    s_edit = row;
    lv_obj_t *card = modal_card(86, 82);
    card_title(card, "Move to room:");
    lv_obj_t *list = lv_obj_create(card);
    lv_obj_set_width(list, lv_pct(100)); lv_obj_set_flex_grow(list, 1);
    lv_obj_set_style_bg_opa(list,0,0); lv_obj_set_style_border_width(list,0,0);
    lv_obj_set_style_pad_all(list,0,0); lv_obj_set_style_pad_row(list,6,0);
    lv_obj_set_flex_flow(list, LV_FLEX_FLOW_COLUMN);
    for (int i=0;i<s_narea;i++){
        lv_obj_t *b = lv_button_create(list);
        lv_obj_set_width(b, lv_pct(100));
        lv_obj_set_style_bg_color(b, lv_color_hex(C_CELL), 0);
        lv_obj_add_event_cb(b, loc_pick_cb, LV_EVENT_CLICKED, (void*)(intptr_t)i);
        lv_obj_t *l = lv_label_create(b); lv_label_set_text(l, s_area[i].name);
        lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0); lv_obj_center(l);
    }
    lv_obj_add_event_cb(card_cancel(card), cancel_cb, LV_EVENT_CLICKED, NULL);
}

// ── Status editor: Active / Hidden / Retired ────────────────────────────────────────────────────────
static void status_pick_cb(lv_event_t *e)
{
    int st = (int)(intptr_t)lv_event_get_user_data(e);
    if (s_edit>=0 && s_edit<s_ndev) stage_status(&s_dev[s_edit], st);
    modal_close();
}
static void open_status_picker(int row)
{
    s_edit = row;
    lv_obj_t *card = modal_card(74, 60);
    card_title(card, "Set status:");
    for (int st=0; st<3; st++){
        lv_obj_t *b = lv_button_create(card);
        lv_obj_set_width(b, lv_pct(100));
        lv_obj_set_style_bg_color(b, lv_color_hex(C_CELL), 0);
        lv_obj_add_event_cb(b, status_pick_cb, LV_EVENT_CLICKED, (void*)(intptr_t)st);
        lv_obj_t *l = lv_label_create(b); lv_label_set_text(l, status_text(st));
        lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0);
        lv_obj_set_style_text_color(l, lv_color_hex(status_color(st)), 0);
        lv_obj_center(l);
    }
    lv_obj_add_event_cb(card_cancel(card), cancel_cb, LV_EVENT_CLICKED, NULL);
}

// ── Name editor: on-screen keyboard ─────────────────────────────────────────────────────────────────
static void name_kb_ready_cb(lv_event_t *e)
{
    lv_obj_t *kb = lv_event_get_target(e);
    lv_obj_t *ta = lv_keyboard_get_textarea(kb);
    const char *txt = ta ? lv_textarea_get_text(ta) : NULL;
    if (s_edit>=0 && s_edit<s_ndev && txt && txt[0]) stage_name(&s_dev[s_edit], txt);
    modal_close();
}
static void name_kb_cancel_cb(lv_event_t *e){ (void)e; modal_close(); }
static void open_name_editor(int row)
{
    s_edit = row;
    lv_obj_t *ov = lv_obj_create(lv_layer_top());
    s_modal = ov;
    lv_obj_set_size(ov, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(ov, lv_color_hex(0x0b1021), 0);
    lv_obj_set_style_bg_opa(ov, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(ov, 0, 0);
    lv_obj_set_style_pad_all(ov, 12, 0);
    lv_obj_clear_flag(ov, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *t = lv_label_create(ov);
    lv_label_set_text(t, "Rename device:");
    lv_obj_set_style_text_font(t, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(t, lv_color_hex(0xffffff), 0);
    lv_obj_align(t, LV_ALIGN_TOP_LEFT, 0, 0);

    lv_obj_t *ta = lv_textarea_create(ov);
    lv_textarea_set_one_line(ta, true);
    lv_textarea_set_text(ta, s_dev[row].name);
    lv_obj_set_width(ta, lv_pct(100));
    lv_obj_align(t, LV_ALIGN_TOP_LEFT, 0, 0);
    lv_obj_align_to(ta, t, LV_ALIGN_OUT_BOTTOM_LEFT, 0, 8);
    lv_obj_set_style_text_font(ta, &lv_font_montserrat_20, 0);

    lv_obj_t *kb = lv_keyboard_create(ov);
    lv_keyboard_set_textarea(kb, ta);
    lv_obj_add_event_cb(kb, name_kb_ready_cb, LV_EVENT_READY, NULL);      // keyboard checkmark
    lv_obj_add_event_cb(kb, name_kb_cancel_cb, LV_EVENT_CANCEL, NULL);    // keyboard close
}

// ── cell taps ───────────────────────────────────────────────────────────────────────────────────────
static void name_cb(lv_event_t *e){ open_name_editor((int)(intptr_t)lv_event_get_user_data(e)); }
static void loc_cb(lv_event_t *e){ open_loc_picker((int)(intptr_t)lv_event_get_user_data(e)); }
static void stat_cb(lv_event_t *e){ open_status_picker((int)(intptr_t)lv_event_get_user_data(e)); }

static void confirm_cb(lv_event_t *e)
{
    int i = (int)(intptr_t)lv_event_get_user_data(e);
    if (i<0 || i>=s_ndev) return;
    struct devrow *d = &s_dev[i];
    if (!(d->st_name||d->st_loc||d->st_status)) return;
    if (s_cb) s_cb(d->id,
                   d->st_loc ? d->s_room : NULL, d->st_loc ? d->s_room_name : NULL,
                   d->st_name ? d->s_name : NULL,
                   d->st_status ? d->s_status : -1);
    d->st_name = d->st_loc = d->st_status = false;
    lv_obj_set_style_border_width(d->name_btn, 0, 0);
    lv_obj_set_style_border_width(d->loc_btn, 0, 0);
    lv_obj_set_style_border_width(d->stat_btn, 0, 0);
    row_refresh_confirm(d);
}

// ── row/header widgets ──────────────────────────────────────────────────────────────────────────────
static lv_obj_t *make_row(lv_obj_t *parent)
{
    lv_obj_t *r = lv_obj_create(parent);
    lv_obj_set_width(r, lv_pct(100));
    lv_obj_set_height(r, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(r, 0, 0);
    lv_obj_set_style_border_width(r, 0, 0);
    lv_obj_set_style_pad_all(r, 0, 0);
    lv_obj_set_style_pad_column(r, 6, 0);
    lv_obj_set_flex_flow(r, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(r, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(r, LV_OBJ_FLAG_SCROLLABLE);
    return r;
}

// an editable cell button (grow-weighted); returns it and hands back its label via *out_lbl
static lv_obj_t *cell_btn(lv_obj_t *row, int grow, const char *txt, uint32_t txt_color,
                         lv_event_cb_t cb, int idx, lv_obj_t **out_lbl)
{
    lv_obj_t *b = lv_button_create(row);
    lv_obj_set_height(b, LV_SIZE_CONTENT);
    lv_obj_set_flex_grow(b, grow);
    lv_obj_set_style_bg_color(b, lv_color_hex(C_CELL), 0);
    lv_obj_set_style_pad_all(b, 10, 0);
    lv_obj_set_style_border_color(b, lv_color_hex(C_STAGE), 0);   // colour set; width 0 until staged
    lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, (void*)(intptr_t)idx);
    lv_obj_t *l = lv_label_create(b);
    lv_label_set_text(l, txt);
    lv_label_set_long_mode(l, LV_LABEL_LONG_DOT);
    lv_obj_set_width(l, lv_pct(100));
    lv_obj_set_style_text_font(l, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(txt_color), 0);
    if (out_lbl) *out_lbl = l;
    return b;
}

static void header_cell(lv_obj_t *row, int grow, const char *txt)
{
    lv_obj_t *l = lv_label_create(row);
    lv_label_set_text(l, txt);
    lv_obj_set_flex_grow(l, grow);
    lv_obj_set_style_text_font(l, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(0x8fb4ff), 0);
}

void ui_devices_render(cJSON *rooms_doc, cJSON *meta_doc, lv_obj_t *parent, ui_devices_commit_cb cb)
{
    if (s_modal) return;              // editing — leave the table alone
    if (any_staged()) return;         // uncommitted edits pending — don't clobber them on a refresh
    s_cb = cb; s_ndev = 0; s_narea = 0;
    lv_obj_clean(parent);

    cJSON *rooms = rooms_doc ? cJSON_GetObjectItem(rooms_doc, "rooms") : NULL;
    if (!cJSON_IsArray(rooms)) {
        lv_obj_t *l = lv_label_create(parent);
        lv_label_set_text(l, "(devices unavailable)");
        lv_obj_set_style_text_color(l, lv_color_hex(0x64748b), 0);
        return;
    }

    // pass 1: rooms -> picker options + active device rows (data only)
    cJSON *room;
    cJSON_ArrayForEach(room, rooms) {
        if (s_narea < MAX_AREA) {
            cJSON *id = cJSON_GetObjectItem(room, "id"), *nm = cJSON_GetObjectItem(room, "name");
            if (cJSON_IsString(id)) {
                snprintf(s_area[s_narea].id, sizeof s_area[0].id, "%s", id->valuestring);
                snprintf(s_area[s_narea].name, sizeof s_area[0].name, "%s",
                         cJSON_IsString(nm) ? nm->valuestring : id->valuestring);
                s_narea++;
            }
        }
        cJSON *devs = cJSON_GetObjectItem(room, "devices");
        if (!cJSON_IsArray(devs)) continue;
        cJSON *rid = cJSON_GetObjectItem(room, "id"), *rnm = cJSON_GetObjectItem(room, "name");
        const char *room_id = cJSON_IsString(rid) ? rid->valuestring : "";
        const char *room_nm = cJSON_IsString(rnm) ? rnm->valuestring : room_id;
        cJSON *d;
        cJSON_ArrayForEach(d, devs) {
            if (s_ndev >= MAX_DEV) break;
            cJSON *did = cJSON_GetObjectItem(d, "device_id"), *dnm = cJSON_GetObjectItem(d, "name");
            if (!cJSON_IsString(did)) continue;
            struct devrow *r = &s_dev[s_ndev++];
            memset(r, 0, sizeof *r);
            snprintf(r->id, sizeof r->id, "%s", did->valuestring);
            snprintf(r->name, sizeof r->name, "%s", cJSON_IsString(dnm) ? dnm->valuestring : did->valuestring);
            snprintf(r->room_id, sizeof r->room_id, "%s", room_id);
            snprintf(r->room_name, sizeof r->room_name, "%s", room_nm);
            r->status = UI_DEV_ACTIVE;
        }
    }

    // pass 2: merge the meta overlay (hidden/retired status; name override; add dropped hidden devices)
    cJSON *meta = meta_doc ? cJSON_GetObjectItem(meta_doc, "meta") : NULL;
    if (cJSON_IsObject(meta)) {
        cJSON *m;
        cJSON_ArrayForEach(m, meta) {
            const char *did = m->string;
            if (!did) continue;
            bool hidden = cJSON_IsTrue(cJSON_GetObjectItem(m, "hidden"));
            bool retired = cJSON_IsTrue(cJSON_GetObjectItem(m, "retired"));
            int st = retired ? UI_DEV_RETIRED : hidden ? UI_DEV_HIDDEN : UI_DEV_ACTIVE;
            cJSON *nm = cJSON_GetObjectItem(m, "name"), *rm = cJSON_GetObjectItem(m, "room");
            int idx = find_dev(did);
            if (idx >= 0) {
                s_dev[idx].status = st;
                if (cJSON_IsString(nm) && nm->valuestring[0])
                    snprintf(s_dev[idx].name, sizeof s_dev[0].name, "%s", nm->valuestring);
            } else if ((hidden || retired) && s_ndev < MAX_DEV) {
                struct devrow *r = &s_dev[s_ndev++];      // hidden/retired -> dropped from rooms; add it back
                memset(r, 0, sizeof *r);
                snprintf(r->id, sizeof r->id, "%s", did);
                snprintf(r->name, sizeof r->name, "%s", cJSON_IsString(nm) && nm->valuestring[0] ? nm->valuestring : did);
                snprintf(r->room_name, sizeof r->room_name, "%s", cJSON_IsString(rm) ? rm->valuestring : "—");
                if (cJSON_IsString(rm)) snprintf(r->room_id, sizeof r->room_id, "%s", rm->valuestring);
                r->status = st;
            }
        }
    }

    // pass 3: build the header + one widget row per device
    lv_obj_t *hdr = make_row(parent);
    header_cell(hdr, 3, "Device");
    header_cell(hdr, 3, "Location");
    header_cell(hdr, 2, "Status");
    lv_obj_t *hpad = lv_label_create(hdr); lv_label_set_text(hpad, ""); lv_obj_set_width(hpad, 64);

    for (int i=0;i<s_ndev;i++){
        struct devrow *r = &s_dev[i];
        lv_obj_t *row = make_row(parent);
        r->name_btn = cell_btn(row, 3, r->name, 0xffffff, name_cb, i, &r->name_lbl);
        r->loc_btn  = cell_btn(row, 3, r->room_name[0]?r->room_name:"—", 0xcbd5e1, loc_cb, i, &r->loc_lbl);
        r->stat_btn = cell_btn(row, 2, status_text(r->status), status_color(r->status), stat_cb, i, &r->stat_lbl);
        lv_obj_t *cf = lv_button_create(row);
        lv_obj_set_size(cf, 64, 44);
        lv_obj_set_style_bg_color(cf, lv_color_hex(C_OK_OFF), 0);
        lv_obj_clear_flag(cf, LV_OBJ_FLAG_CLICKABLE);          // lit only when the row has a staged change
        lv_obj_add_event_cb(cf, confirm_cb, LV_EVENT_CLICKED, (void*)(intptr_t)i);
        lv_obj_t *cl = lv_label_create(cf); lv_label_set_text(cl, LV_SYMBOL_OK);
        lv_obj_set_style_text_font(cl, &lv_font_montserrat_20, 0); lv_obj_center(cl);
        r->confirm = cf;
    }

    if (s_ndev == 0) {
        lv_obj_t *l = lv_label_create(parent);
        lv_label_set_text(l, "(no devices)");
        lv_obj_set_style_text_color(l, lv_color_hex(0x64748b), 0);
    }
}
