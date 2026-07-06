// House map (see ui_map.h). Renders /api/v1/rooms as the actual floor plan: each room's polygon
// drawn as vector wall outlines (lv_line — no framebuffer, so the PSRAM draw budget is untouched),
// with a compact live-reading label inside. House-space coords are scaled to the screen. Monolithic
// rooms (attic/crawlspace — no polygon) render as chips in a bottom strip.
#include "ui/ui_map.h"
#include "ui/ui_format.h"   // ascii_fold (font is ASCII-only; drop non-ASCII glyphs)
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include "lvgl.h"
#include "esp_log.h"

static const char *TAG = "ui.map";

#define MAX_ROOMS     32
#define MAX_RINGS     48      // a composite room (e.g. an L-shaped hall) contributes several rings
#define MAX_RING_PTS  20      // points per ring incl. the closing repeat
#define LBL_W         128
#define LBL_H         46
#define COL_W         150     // right-side column for monolithic rooms (attic/crawlspace); frees the
                              // full screen height for the (tall, narrow) floor plan to expand into
#define PAD           14
#define WALL_COL      0x7fa9d9
#define ACT_WALL_COL  0xd9a85c

static struct { char id[28]; char name[28]; } s_reg[MAX_ROOMS];
static int s_nreg;
static lv_point_precise_t s_ring[MAX_RINGS][MAX_RING_PTS];   // point pool (lv_line keeps the pointer)
static int s_nring;
static ui_map_room_cb s_cb;

// scale state (house-space -> screen), set per render
static double s_mnx, s_mny, s_scale;

static int scr_x(double hx) { return (int)(PAD + (hx - s_mnx) * s_scale); }
static int scr_y(double hy) { return (int)(PAD + (hy - s_mny) * s_scale); }

static void room_clicked_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx < 0 || idx >= s_nreg || !s_cb) return;
    ESP_LOGI(TAG, "tap -> room %s", s_reg[idx].id);
    s_cb(s_reg[idx].id, s_reg[idx].name);
}

// --- geometry helpers ------------------------------------------------------
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
            double tf = disp_val("C", t->valuedouble);   // panel-local Fahrenheit (data stays SI)
            if (cJSON_IsNumber(h)) snprintf(out, n, "%.0f  %.0f%%", tf, h->valuedouble);
            else                   snprintf(out, n, "%.0f", tf);
            return;
        }
    }
}

// --- draw one polygon ring as a closed wall outline ------------------------
static void draw_ring(cJSON *ring, lv_obj_t *parent, uint32_t col)
{
    if (!cJSON_IsArray(ring) || s_nring >= MAX_RINGS) return;
    int np = cJSON_GetArraySize(ring);
    if (np < 2 || np > MAX_RING_PTS - 1) return;
    lv_point_precise_t *pts = s_ring[s_nring];
    int i = 0;
    cJSON *pt;
    cJSON_ArrayForEach(pt, ring) {
        if (!cJSON_IsArray(pt) || cJSON_GetArraySize(pt) != 2) continue;
        pts[i].x = scr_x(cJSON_GetArrayItem(pt, 0)->valuedouble);
        pts[i].y = scr_y(cJSON_GetArrayItem(pt, 1)->valuedouble);
        i++;
    }
    if (i < 2) return;
    pts[i] = pts[0];            // close the loop
    i++;

    lv_obj_t *line = lv_line_create(parent);
    lv_obj_set_pos(line, 0, 0);
    lv_line_set_points(line, pts, i);
    lv_obj_set_style_line_color(line, lv_color_hex(col), 0);
    lv_obj_set_style_line_width(line, 3, 0);
    lv_obj_set_style_line_rounded(line, true, 0);
    lv_obj_clear_flag(line, LV_OBJ_FLAG_CLICKABLE);   // taps go to the room label, not the wall
    s_nring++;
}

// --- compact room label (name + glance/count), clickable = the tap target --
static void make_label(lv_obj_t *parent, cJSON *room, int reg_idx, int cx, int cy, bool act, bool strip)
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

    lv_obj_t *box = lv_obj_create(parent);
    lv_obj_set_size(box, LBL_W, LBL_H);
    int x = cx - LBL_W / 2, y = cy - LBL_H / 2;
    lv_obj_set_pos(box, x, y);
    lv_obj_set_style_bg_color(box, lv_color_hex(0x0b1021), 0);
    lv_obj_set_style_bg_opa(box, strip ? LV_OPA_COVER : 190, 0);   // semi-transparent so walls read through
    lv_obj_set_style_border_width(box, strip ? 2 : 0, 0);
    lv_obj_set_style_border_color(box, lv_color_hex(act ? ACT_WALL_COL : WALL_COL), 0);
    lv_obj_set_style_radius(box, 6, 0);
    lv_obj_set_style_pad_all(box, 4, 0);
    lv_obj_set_flex_flow(box, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(box, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(box, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(box, room_clicked_cb, LV_EVENT_CLICKED, (void *)(intptr_t)reg_idx);

    lv_obj_t *t = lv_label_create(box);
    lv_label_set_text(t, folded);
    lv_obj_set_style_text_font(t, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(t, lv_color_hex(0xffffff), 0);
    lv_label_set_long_mode(t, LV_LABEL_LONG_DOT);
    lv_obj_set_width(t, lv_pct(100));

    lv_obj_t *sub = lv_label_create(box);
    if (glance[0]) lv_label_set_text(sub, glance);
    else if (na)   lv_label_set_text_fmt(sub, "%ds %da", ns, na);
    else           lv_label_set_text_fmt(sub, "%d sen", ns);
    lv_obj_set_style_text_font(sub, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(sub, lv_color_hex(act ? ACT_WALL_COL : 0x8fb4ff), 0);
}

// --- render ----------------------------------------------------------------
void ui_map_render(cJSON *root, lv_obj_t *parent, ui_map_room_cb cb)
{
    s_cb = cb;
    s_nreg = 0;
    s_nring = 0;
    lv_obj_clean(parent);
    lv_obj_set_layout(parent, LV_LAYOUT_NONE);

    cJSON *rooms = cJSON_GetObjectItem(root, "rooms");
    if (!cJSON_IsArray(rooms)) { ESP_LOGW(TAG, "no rooms[]"); return; }

    double mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
    cJSON *r;
    cJSON_ArrayForEach(r, rooms) room_bounds(cJSON_GetObjectItem(r, "geometry"), &mnx, &mny, &mxx, &mxy);
    bool have_space = (mxx > mnx && mxy > mny);

    int pw = lv_obj_get_width(parent), ph = lv_obj_get_height(parent);
    if (pw <= 0) pw = 780;
    if (ph <= 0) ph = 1040;
    int mapw = pw - COL_W;                       // floor plan = left area; right column = monolithic rooms
    s_mnx = mnx; s_mny = mny;
    double sx = have_space ? (double)(mapw - 2 * PAD) / (mxx - mnx) : 1;
    double sy = have_space ? (double)(ph - 2 * PAD) / (mxy - mny) : 1;   // full screen height now
    s_scale = sx < sy ? sx : sy;                 // uniform, preserve aspect

    // pass 1: walls (so labels draw on top)
    if (have_space) {
        cJSON_ArrayForEach(r, rooms) {
            cJSON *geo = cJSON_GetObjectItem(r, "geometry");
            if (!cJSON_IsObject(geo)) continue;
            cJSON *counts = cJSON_GetObjectItem(r, "counts");
            cJSON *ca = cJSON_IsObject(counts) ? cJSON_GetObjectItem(counts, "actuators") : NULL;
            uint32_t col = (cJSON_IsNumber(ca) && ca->valueint > 0) ? ACT_WALL_COL : WALL_COL;
            cJSON *poly = cJSON_GetObjectItem(geo, "poly");
            if (cJSON_IsArray(poly)) draw_ring(poly, parent, col);
            cJSON *polys = cJSON_GetObjectItem(geo, "polys");
            if (cJSON_IsArray(polys)) {
                cJSON *ring;
                cJSON_ArrayForEach(ring, polys) draw_ring(ring, parent, col);
            }
        }
    }

    // pass 2: labels (placed rooms at centroids; monolithic/geometry-less-with-devices -> right column)
    int col_y = PAD + LBL_H / 2;
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
        bool act = na > 0;

        const cJSON *ji = cJSON_GetObjectItem(r, "id");
        const cJSON *jn = cJSON_GetObjectItem(r, "name");
        int idx = s_nreg;
        strncpy(s_reg[idx].id, cJSON_IsString(ji) ? ji->valuestring : "?", sizeof s_reg[idx].id - 1);
        s_reg[idx].id[sizeof s_reg[idx].id - 1] = 0;
        strncpy(s_reg[idx].name, cJSON_IsString(jn) ? jn->valuestring : s_reg[idx].id, sizeof s_reg[idx].name - 1);
        s_reg[idx].name[sizeof s_reg[idx].name - 1] = 0;

        double lx, ly;
        if (have_space && room_label(geo, &lx, &ly)) {
            int x = scr_x(lx), y = scr_y(ly);
            if (x < PAD + LBL_W / 2) x = PAD + LBL_W / 2;
            if (x > mapw - PAD - LBL_W / 2) x = mapw - PAD - LBL_W / 2;
            if (y < PAD + LBL_H / 2) y = PAD + LBL_H / 2;
            if (y > ph - PAD - LBL_H / 2) y = ph - PAD - LBL_H / 2;
            make_label(parent, r, idx, x, y, act, false);
            s_nreg++;
        } else if (ns + na > 0) {
            if (col_y + LBL_H / 2 > ph - PAD) continue;    // right column full
            make_label(parent, r, idx, pw - COL_W / 2, col_y, act, true);
            col_y += LBL_H + 12;
            s_nreg++;
        }
    }
    ESP_LOGI(TAG, "rendered floor plan: %d rooms, %d wall rings (space=%d)", s_nreg, s_nring, have_space);
}
