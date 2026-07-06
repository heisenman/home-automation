// House map (see ui_map.h). Renders /api/v1/rooms as room chips positioned at their real geometry
// centroids, scaled from house-space to the screen. Existing lv_obj/label infra only — no lv_canvas.
#include "ui/ui_map.h"
#include "ui/ui_format.h"   // ascii_fold (font is ASCII-only; drop non-ASCII glyphs)
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include "lvgl.h"
#include "esp_log.h"

static const char *TAG = "ui.map";

#define MAX_ROOMS   32
#define CHIP_W      150
#define CHIP_H      66
#define STRIP_H     72     // bottom strip for monolithic (geometry-less) rooms
#define PAD         14

static struct { char id[28]; } s_reg[MAX_ROOMS];
static int s_nreg;
static ui_map_room_cb s_cb;

static void room_clicked_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx < 0 || idx >= s_nreg || !s_cb) return;
    ESP_LOGI(TAG, "tap -> room %s", s_reg[idx].id);
    s_cb(s_reg[idx].id);
}

// --- geometry helpers ------------------------------------------------------
// A room's label centroid (house-space). Returns false if it has no polygon geometry (monolithic).
static bool room_label(cJSON *geo, double *lx, double *ly)
{
    if (!cJSON_IsObject(geo)) return false;
    cJSON *lab = cJSON_GetObjectItem(geo, "label");
    if (cJSON_IsArray(lab) && cJSON_GetArraySize(lab) == 2) {
        *lx = cJSON_GetArrayItem(lab, 0)->valuedouble;
        *ly = cJSON_GetArrayItem(lab, 1)->valuedouble;
        return true;
    }
    return false;
}

// Grow the house-space bounding box by every coordinate we can find (labels + polygons), so we can
// scale to the screen without depending on the endpoint carrying the top-level space dims.
static void scan_pts(cJSON *arr, double *mnx, double *mny, double *mxx, double *mxy)
{
    cJSON *pt;
    cJSON_ArrayForEach(pt, arr) {
        if (cJSON_IsArray(pt) && cJSON_GetArraySize(pt) == 2) {
            double x = cJSON_GetArrayItem(pt, 0)->valuedouble;
            double y = cJSON_GetArrayItem(pt, 1)->valuedouble;
            if (x < *mnx) *mnx = x;
            if (x > *mxx) *mxx = x;
            if (y < *mny) *mny = y;
            if (y > *mxy) *mxy = y;
        }
    }
}

static void room_bounds(cJSON *geo, double *mnx, double *mny, double *mxx, double *mxy)
{
    if (!cJSON_IsObject(geo)) return;
    double lx, ly;
    if (room_label(geo, &lx, &ly)) {
        if (lx < *mnx) *mnx = lx;
        if (lx > *mxx) *mxx = lx;
        if (ly < *mny) *mny = ly;
        if (ly > *mxy) *mxy = ly;
    }
    cJSON *poly = cJSON_GetObjectItem(geo, "poly");
    if (cJSON_IsArray(poly)) scan_pts(poly, mnx, mny, mxx, mxy);
    cJSON *polys = cJSON_GetObjectItem(geo, "polys");
    if (cJSON_IsArray(polys)) {
        cJSON *ring;
        cJSON_ArrayForEach(ring, polys) if (cJSON_IsArray(ring)) scan_pts(ring, mnx, mny, mxx, mxy);
    }
}

// The room's headline reading for the glance chip, ASCII-only (font drops °). "" if none.
static void room_glance(cJSON *devs, char *out, size_t n)
{
    out[0] = 0;
    cJSON *d;
    cJSON_ArrayForEach(d, devs) {
        cJSON *m = cJSON_GetObjectItem(d, "metrics");
        if (!cJSON_IsObject(m)) continue;
        cJSON *t = cJSON_GetObjectItem(m, "temperature_c");
        cJSON *h = cJSON_GetObjectItem(m, "humidity_pct");
        if (cJSON_IsNumber(t)) {
            if (cJSON_IsNumber(h)) snprintf(out, n, "%.1f  %.0f%%", t->valuedouble, h->valuedouble);
            else                   snprintf(out, n, "%.1f", t->valuedouble);
            return;
        }
    }
}

// --- one chip --------------------------------------------------------------
static void make_chip(lv_obj_t *parent, cJSON *room, int reg_idx, int x, int y, bool actuator_room)
{
    const cJSON *jn = cJSON_GetObjectItem(room, "name");
    const cJSON *ji = cJSON_GetObjectItem(room, "id");
    const char *name = cJSON_IsString(jn) ? jn->valuestring : (cJSON_IsString(ji) ? ji->valuestring : "?");
    cJSON *counts = cJSON_GetObjectItem(room, "counts");
    int ns = 0, na = 0;
    if (cJSON_IsObject(counts)) {
        cJSON *s = cJSON_GetObjectItem(counts, "sensors"), *a = cJSON_GetObjectItem(counts, "actuators");
        ns = cJSON_IsNumber(s) ? s->valueint : 0;
        na = cJSON_IsNumber(a) ? a->valueint : 0;
    }
    char glance[32]; room_glance(cJSON_GetObjectItem(room, "devices"), glance, sizeof glance);
    char folded[40]; ascii_fold(name, folded, sizeof folded);

    lv_obj_t *chip = lv_obj_create(parent);
    lv_obj_set_size(chip, CHIP_W, CHIP_H);
    lv_obj_set_pos(chip, x, y);
    lv_obj_set_style_bg_color(chip, lv_color_hex(actuator_room ? 0x2a2411 : 0x16204a), 0);
    lv_obj_set_style_border_color(chip, lv_color_hex(actuator_room ? 0xb4823c : 0x2f7e7a), 0);
    lv_obj_set_style_border_width(chip, 2, 0);
    lv_obj_set_style_radius(chip, 10, 0);
    lv_obj_set_style_pad_all(chip, 6, 0);
    lv_obj_set_flex_flow(chip, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(chip, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(chip, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(chip, room_clicked_cb, LV_EVENT_CLICKED, (void *)(intptr_t)reg_idx);

    lv_obj_t *t = lv_label_create(chip);
    lv_label_set_text(t, folded);
    lv_obj_set_style_text_font(t, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(t, lv_color_hex(0xffffff), 0);
    lv_label_set_long_mode(t, LV_LABEL_LONG_DOT);
    lv_obj_set_width(t, lv_pct(100));

    lv_obj_t *sub = lv_label_create(chip);
    if (glance[0]) lv_label_set_text(sub, glance);
    else if (na)   lv_label_set_text_fmt(sub, "%ds  %da", ns, na);
    else           lv_label_set_text_fmt(sub, "%d sen", ns);
    lv_obj_set_style_text_color(sub, lv_color_hex(actuator_room ? 0xd9a85c : 0x8fb4ff), 0);
}

// --- render ----------------------------------------------------------------
void ui_map_render(cJSON *root, lv_obj_t *parent, ui_map_room_cb cb)
{
    s_cb = cb;
    s_nreg = 0;
    lv_obj_clean(parent);
    lv_obj_set_layout(parent, LV_LAYOUT_NONE);   // absolute positioning of chips

    cJSON *rooms = cJSON_GetObjectItem(root, "rooms");
    if (!cJSON_IsArray(rooms)) { ESP_LOGW(TAG, "no rooms[]"); return; }

    // pass 1: house-space bounds over every placed room
    double mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
    cJSON *r;
    cJSON_ArrayForEach(r, rooms) room_bounds(cJSON_GetObjectItem(r, "geometry"), &mnx, &mny, &mxx, &mxy);
    bool have_space = (mxx > mnx && mxy > mny);

    int pw = lv_obj_get_width(parent), ph = lv_obj_get_height(parent);
    if (pw <= 0) pw = 780;
    if (ph <= 0) ph = 1040;
    int map_h = ph - STRIP_H;                    // reserve the bottom strip for monolithic rooms
    double sx = have_space ? (pw - CHIP_W - 2 * PAD) / (mxx - mnx) : 1;
    double sy = have_space ? (map_h - CHIP_H - 2 * PAD) / (mxy - mny) : 1;
    double s = sx < sy ? sx : sy;                // uniform scale, preserve aspect

    // pass 2: place chips
    int strip_x = PAD;
    cJSON_ArrayForEach(r, rooms) {
        if (s_nreg >= MAX_ROOMS) break;
        cJSON *geo = cJSON_GetObjectItem(r, "geometry");
        cJSON *counts = cJSON_GetObjectItem(r, "counts");
        int ns = 0, na = 0;
        if (cJSON_IsObject(counts)) {
            cJSON *cs = cJSON_GetObjectItem(counts, "sensors");
            cJSON *cna = cJSON_GetObjectItem(counts, "actuators");
            ns = cJSON_IsNumber(cs) ? cs->valueint : 0;
            na = cJSON_IsNumber(cna) ? cna->valueint : 0;
        }
        int nd = ns + na;
        bool act = na > 0;

        const cJSON *ji = cJSON_GetObjectItem(r, "id");
        int idx = s_nreg;
        strncpy(s_reg[idx].id, cJSON_IsString(ji) ? ji->valuestring : "?", sizeof s_reg[idx].id - 1);
        s_reg[idx].id[sizeof s_reg[idx].id - 1] = 0;

        double lx, ly;
        if (have_space && room_label(geo, &lx, &ly)) {
            int x = (int)(PAD + (lx - mnx) * s) - CHIP_W / 2;
            int y = (int)(PAD + (ly - mny) * s) - CHIP_H / 2;
            if (x < PAD) x = PAD;
            if (x > pw - CHIP_W - PAD) x = pw - CHIP_W - PAD;
            if (y < PAD) y = PAD;
            if (y > map_h - CHIP_H - PAD) y = map_h - CHIP_H - PAD;
            make_chip(parent, r, idx, x, y, act);
            s_nreg++;
        } else if (nd > 0) {
            // monolithic / geometry-less room WITH devices -> bottom strip (skip empty ones)
            if (strip_x + CHIP_W > pw - PAD) continue;
            make_chip(parent, r, idx, strip_x, map_h + (STRIP_H - CHIP_H) / 2, act);
            strip_x += CHIP_W + 10;
            s_nreg++;
        }
    }
    ESP_LOGI(TAG, "rendered %d room chips (space=%d)", s_nreg, have_space);
}
