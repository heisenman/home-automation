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
#define C_NORMAL      0x8fb4ff   // sensor / count text
#define C_AMBER       0xf59e0b   // averaged climate (no primary configured)
#define C_RED         0xef4444   // averaged + divergent, or out-of-range
#define C_ACTIVE      0x4ade80   // actuator running (green)
#define C_IDLE        0x64748b   // actuator idle (dim slate)
#define ML_MAX        14         // max content lines below the room name
#define ML_LEN        56

#define MAX_DEVS      48

static struct { char id[28]; char name[28]; } s_reg[MAX_ROOMS];
static int s_nreg;
static lv_point_precise_t s_ring[MAX_RINGS][MAX_RING_PTS];   // point pool (lv_line keeps the pointer)
static int s_nring;
static ui_map_room_cb s_cb;

// device registry for the spatial room-zoom (tap -> id/name, since cJSON is freed after render)
static struct { char id[40]; char name[40]; } s_dreg[MAX_DEVS];
static int s_ndreg;
static ui_map_device_cb s_dcb;

// scale state (house-space -> screen), set per render. s_xoff/s_yoff center a single zoomed room.
static double s_mnx, s_mny, s_scale;
static int s_xoff, s_yoff;

static int scr_x(double hx) { return (int)(PAD + s_xoff + (hx - s_mnx) * s_scale); }
static int scr_y(double hy) { return (int)(PAD + s_yoff + (hy - s_mny) * s_scale); }

static void room_clicked_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx < 0 || idx >= s_nreg || !s_cb) return;
    ESP_LOGI(TAG, "tap -> room %s", s_reg[idx].id);
    s_cb(s_reg[idx].id, s_reg[idx].name);
}

static void device_clicked_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx < 0 || idx >= s_ndreg || !s_dcb) return;
    ESP_LOGI(TAG, "tap -> device %s", s_dreg[idx].id);
    s_dcb(s_dreg[idx].id, s_dreg[idx].name);
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

// --- adaptive room-fill helpers (docs/design/map-room-fill.md) -------------
// Render the richest tier that fits the room's box: 0a (every device, all metrics) -> 0b (every
// device, core metrics) -> 1 (climate glance + counts) -> 2 (counts). Fit is measured with
// lv_text_get_size against the room's on-screen bbox; the font stays fixed (legibility at distance).

// wrapped px size of `s` at `max_w` in `font`. Returns height; *w gets the used width.
static int text_wh_f(const char *s, int max_w, const lv_font_t *font, int *w)
{
    lv_point_t sz;
    lv_text_get_size(&sz, s, font, 0, 1, max_w > 0 ? max_w : LV_COORD_MAX, LV_TEXT_FLAG_NONE);
    if (w) *w = sz.x;
    return sz.y;
}
// default 14pt measure (tier selection floors on the smallest font so a tier that fits at 14 always fits).
static int text_wh(const char *s, int max_w, int *w) { return text_wh_f(s, max_w, &lv_font_montserrat_14, w); }

// Any metric on this device out of its normal range (server out_of_range map, sparse {metric:True}).
static bool dev_alert(cJSON *dev)
{
    cJSON *o = cJSON_GetObjectItem(dev, "out_of_range");
    return cJSON_IsObject(o) && o->child != NULL;
}

static bool dev_is_actuator(cJSON *dev)
{
    cJSON *r = cJSON_GetObjectItem(dev, "role");
    return cJSON_IsString(r) && strcmp(r->valuestring, "actuator") == 0;
}

// Row color for a device: out-of-range red wins; else an actuator shows running(green)/idle(dim)
// from its server state; sensors are normal. Inert until the BFF ships dev.state (-> normal).
static uint32_t dev_color(cJSON *dev)
{
    if (dev_alert(dev)) return C_RED;
    if (dev_is_actuator(dev)) {
        cJSON *st = cJSON_GetObjectItem(dev, "state");
        if (cJSON_IsObject(st))
            return cJSON_IsTrue(cJSON_GetObjectItem(st, "running")) ? C_ACTIVE : C_IDLE;
    }
    return C_NORMAL;
}

// Raw sensor outputs (e.g. voc_raw) are noisy and unbounded — never surfaced on the map. Only the
// interpreted INDEX metrics (voc_index, eco2, tvoc, aqi, ...) and the climate readings are shown.
// Keyed off the "_raw" suffix so new raw metrics are excluded without a code change (Hugh 2026-07-07).
static bool is_raw_key(const char *k)
{
    size_t n = strlen(k);
    return n >= 4 && strcmp(k + n - 4, "_raw") == 0;
}

// Terse unit for the map glance: humidity's "%RH" collapses to "%" (the "nnF mm%" pair stays ~7 chars).
static const char *map_unit(const char *u) { return (u && u[0] == '%') ? "%" : u; }

// Compact marker (no device name), for the house view when full chips would crowd a room. Actuator
// -> run glyph. Sensor -> its FIRST TWO non-raw readings at prec 0 (catalog order leads with temp +
// humidity -> "72F 44%"; an sgp30 -> "1143ppm 269ppb"; an sgp40 -> "115"). Nothing numeric -> a short
// name stem. Placement disambiguates which marker is which; the full reading is one tap away.
static void dev_compact(cJSON *dev, char *out, size_t n)
{
    cJSON *st = cJSON_GetObjectItem(dev, "state");
    if (dev_is_actuator(dev) && cJSON_IsObject(st)) {
        snprintf(out, n, "%s", cJSON_IsTrue(cJSON_GetObjectItem(st, "running"))
                 ? LV_SYMBOL_PLAY : LV_SYMBOL_PAUSE);
        return;
    }
    cJSON *m = cJSON_GetObjectItem(dev, "metrics");
    if (cJSON_IsObject(m)) {
        int cnt;
        const struct mfmt *cat = ui_format_catalog(&cnt);
        int off = 0, shown = 0; out[0] = 0;
        for (int i = 0; i < cnt && off < (int)n - 1; i++) {
            if (is_raw_key(cat[i].key)) continue;
            bool present;
            double v = metric_of(m, cat[i].key, &present);
            if (!present) continue;
            const char *u = map_unit(disp_unit(cat[i].unit));
            off += snprintf(out + off, n - off, "%s%.0f%s", off ? " " : "",
                            disp_val(cat[i].unit, v), u ? u : "");
            if (++shown >= 2) break;                    // first two readings — the glance pair
        }
        if (shown > 0) return;
    }
    const cJSON *jn = cJSON_GetObjectItem(dev, "name");
    const cJSON *jid = cJSON_GetObjectItem(dev, "device_id");
    const char *nm = cJSON_IsString(jn) ? jn->valuestring
                     : (cJSON_IsString(jid) ? jid->valuestring : "?");
    char f[16]; ascii_fold(nm, f, sizeof f); if (strlen(f) > 8) f[8] = 0;
    snprintf(out, n, "%s", f);
}

// A house-view chip: `text` colored by state, clickable -> zooms the room (reg_idx), positioned at
// (cx,cy) but CLAMPED so its whole box stays inside the room's px bbox [x0,y0]-[x1,y1]. The clamp is
// what keeps a chip (or the room name) from spilling past the walls into a neighbor.
static void house_chip(lv_obj_t *parent, const char *text, uint32_t col, bool is_name,
                       int cx, int cy, int reg_idx, int x0, int y0, int x1, int y1)
{
    int w, h = text_wh(text, 0, &w);
    int bw = w + 10, bh = h + 6;
    int px = cx - bw / 2, py = cy - bh / 2;
    if (x1 > x0) { if (px + bw > x1) px = x1 - bw; if (px < x0) px = x0; }
    if (y1 > y0) { if (py + bh > y1) py = y1 - bh; if (py < y0) py = y0; }

    lv_obj_t *box = lv_obj_create(parent);
    lv_obj_set_size(box, bw, bh);
    lv_obj_set_pos(box, px, py);
    lv_obj_set_style_bg_color(box, lv_color_hex(0x0b1021), 0);
    lv_obj_set_style_bg_opa(box, is_name ? 150 : 200, 0);
    lv_obj_set_style_border_width(box, 0, 0);
    lv_obj_set_style_radius(box, 5, 0);
    lv_obj_set_style_pad_all(box, 2, 0);
    lv_obj_clear_flag(box, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(box, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(box, room_clicked_cb, LV_EVENT_CLICKED, (void *)(intptr_t)reg_idx);

    lv_obj_t *t = lv_label_create(box);
    lv_label_set_text(t, text);
    lv_obj_set_style_text_font(t, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(t, lv_color_hex(col), 0);
    lv_obj_center(t);
}

// One device's glance line: short name + its metrics in catalog (priority) order. core_only drops
// the secondary metrics (the 0a->0b degrade, driven by the server metric_spec.core flag). Always
// emits at least the name (honors "all devices").
static void dev_line(cJSON *dev, bool core_only, const char *room_id, char *out, size_t n)
{
    const cJSON *jn = cJSON_GetObjectItem(dev, "name");
    const cJSON *jid = cJSON_GetObjectItem(dev, "device_id");
    // fall back to device_id (matches the PWA), NOT device_type — an unnamed device's device_type is
    // "unknown", which read as a bogus label (e.g. host_210 showing "unknown").
    bool from_id = !cJSON_IsString(jn);
    const char *nm = cJSON_IsString(jn) ? jn->valuestring
                     : (cJSON_IsString(jid) ? jid->valuestring : "?");
    char folded[24];
    ascii_fold(nm, folded, sizeof folded);
    // when the label is the device_id, strip a redundant trailing "_<room_id>" so two devices in the
    // same room disambiguate on their distinctive stem (meter_living_room -> "meter", meter_pro -> "meter_pro").
    if (from_id && room_id && room_id[0]) {
        size_t fl = strlen(folded), rl = strlen(room_id);
        if (fl > rl + 1 && folded[fl - rl - 1] == '_' && strcmp(folded + fl - rl, room_id) == 0)
            folded[fl - rl - 1] = 0;
    }
    if (strlen(folded) > 13) folded[13] = 0;          // keep the line compact (distinctive stem shows)

    // Actuator: show ACTION (running glyph + server status string), not its onboard reading — the
    // room's climate line already covers ambient. Defensive: no dev.state yet -> fall through to
    // metrics (no regression until the BFF ships it).
    cJSON *st = cJSON_GetObjectItem(dev, "state");
    if (dev_is_actuator(dev) && cJSON_IsObject(st)) {
        bool running = cJSON_IsTrue(cJSON_GetObjectItem(st, "running"));
        cJSON *stat = cJSON_GetObjectItem(st, "status");
        char sf[24]; ascii_fold(cJSON_IsString(stat) ? stat->valuestring : "", sf, sizeof sf);
        snprintf(out, n, "%s %s%s%s", running ? LV_SYMBOL_PLAY : LV_SYMBOL_PAUSE,
                 folded, sf[0] ? " " : "", sf);
        return;
    }

    int off = snprintf(out, n, "%s", folded);
    cJSON *m = cJSON_GetObjectItem(dev, "metrics");
    if (!cJSON_IsObject(m)) return;
    int cnt;
    const struct mfmt *cat = ui_format_catalog(&cnt);
    for (int i = 0; i < cnt && off < (int)n - 1; i++) {
        if (core_only && !cat[i].core) continue;
        if (is_raw_key(cat[i].key)) continue;          // index metrics only — never raw
        bool present;
        double v = metric_of(m, cat[i].key, &present);
        if (!present) continue;
        double dv = disp_val(cat[i].unit, v);          // panel-local Fahrenheit; data stays SI
        const char *u = map_unit(disp_unit(cat[i].unit));
        off += snprintf(out + off, n - off, " %.*f%s", cat[i].prec, dv, u ? u : "");
    }
}

// Tier-1 climate glance. Prefers the BFF-authored room.climate (value + confidence); falls back to
// the first sensor's temp/hum until the BFF ships it. *color set per confidence (amber=averaged,
// red=averaged+divergent). Returns false if no temperature is available anywhere in the room.
static bool climate_line(cJSON *room, char *out, size_t n, uint32_t *color)
{
    *color = C_NORMAL;
    cJSON *cl = cJSON_GetObjectItem(room, "climate");
    if (cJSON_IsObject(cl)) {
        cJSON *conf = cJSON_GetObjectItem(cl, "confidence");
        const char *cs = cJSON_IsString(conf) ? conf->valuestring : "";
        if (strcmp(cs, "averaged") == 0) *color = C_AMBER;
        else if (strcmp(cs, "averaged_divergent") == 0) *color = C_RED;
        cJSON *val = cJSON_GetObjectItem(cl, "value");
        if (cJSON_IsObject(val)) {
            bool ht, hh;
            double t = metric_of(val, "temperature_c", &ht);
            double h = metric_of(val, "humidity_pct", &hh);
            char pfx = (*color == C_RED) ? '!' : ' ';   // divergent gets a glyph, not just a tint
            if (ht && hh) { snprintf(out, n, "%c%.0f %.0f%%", pfx, disp_val("C", t), h); return true; }
            if (ht)       { snprintf(out, n, "%c%.0f", pfx, disp_val("C", t)); return true; }
        }
    }
    cJSON *devs = cJSON_GetObjectItem(room, "devices"), *d;
    cJSON_ArrayForEach(d, devs) {
        cJSON *m = cJSON_GetObjectItem(d, "metrics");
        if (!cJSON_IsObject(m)) continue;
        bool ht, hh;
        double t = metric_of(m, "temperature_c", &ht);
        double h = metric_of(m, "humidity_pct", &hh);
        if (ht && hh) { snprintf(out, n, "%.0f %.0f%%", disp_val("C", t), h); return true; }
        if (ht)       { snprintf(out, n, "%.0f", disp_val("C", t)); return true; }
    }
    return false;
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

// The room's on-screen bbox (px), so the label can be sized to fit the room, not overflow neighbours.
static void room_screen_bbox(cJSON *geo, int *bw, int *bh)
{
    double mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
    cJSON *poly = cJSON_GetObjectItem(geo, "poly");
    if (cJSON_IsArray(poly)) scan_pts(poly, &mnx, &mny, &mxx, &mxy);
    cJSON *polys = cJSON_GetObjectItem(geo, "polys");
    if (cJSON_IsArray(polys)) {
        cJSON *ring;
        cJSON_ArrayForEach(ring, polys) if (cJSON_IsArray(ring)) scan_pts(ring, &mnx, &mny, &mxx, &mxy);
    }
    if (mxx > mnx) { *bw = scr_x(mxx) - scr_x(mnx); *bh = scr_y(mxy) - scr_y(mny); }
    else { *bw = LBL_W; *bh = LBL_H; }
}

// --- room label: adaptive tier fill, sized to fit `bw`x`bh` (the room). clickable = tap target ---
static void make_label(lv_obj_t *parent, cJSON *room, int reg_idx, int cx, int cy, bool act, bool strip,
                       int bw, int bh, int cbx0, int cby0, int cbx1, int cby1)
{
    const cJSON *jn = cJSON_GetObjectItem(room, "name");
    const cJSON *ji = cJSON_GetObjectItem(room, "id");
    const char *name = cJSON_IsString(jn) ? jn->valuestring : (cJSON_IsString(ji) ? ji->valuestring : "?");
    const char *rid = cJSON_IsString(ji) ? ji->valuestring : "";
    cJSON *counts = cJSON_GetObjectItem(room, "counts");
    int ns = 0, na = 0;
    if (cJSON_IsObject(counts)) {
        cJSON *s = cJSON_GetObjectItem(counts, "sensors"), *a = cJSON_GetObjectItem(counts, "actuators");
        ns = cJSON_IsNumber(s) ? s->valueint : 0;
        na = cJSON_IsNumber(a) ? a->valueint : 0;
    }
    cJSON *devs = cJSON_GetObjectItem(room, "devices");
    int ndev = cJSON_IsArray(devs) ? cJSON_GetArraySize(devs) : 0;
    char folded[40]; ascii_fold(name, folded, sizeof folded);

    // Usable interior (px). The old 150x74 clamp is gone — the box grows to the chosen tier's
    // content, bounded only by the room's on-screen bbox. Floor the width so a sliver room still
    // gets a legible count.
    const int inset = 6;
    int usable_w = bw - inset; if (usable_w < 50) usable_w = 50;
    int usable_h = bh - inset; if (usable_h < 20) usable_h = 20;
    int name_w = 0, name_h = text_wh(folded, usable_w, &name_w);
    int budget = usable_h - name_h;                    // px left for content lines below the name

    // Pick the richest tier that fits — WIDTH and height. Content rows are single-line: a row wider
    // than the room degrades the WHOLE tier (a narrow room drops to the nameless climate line, which
    // fits) rather than clipping mid-value. lines[]/lcol[] hold the content lines under the name.
    char lines[ML_MAX][ML_LEN];
    uint32_t lcol[ML_MAX];
    int nl = 0, tier = -1, content_w = name_w;         // 0a=0 0b=1 climate=2 counts=3

    // Tier 0a / 0b — one line per device (all-or-nothing on the device LIST; metrics may degrade).
    for (int mode = 0; mode <= 1 && ndev > 0 && tier < 0; mode++) {   // 0=full(0a) 1=core(0b)
        int h = 0, maxw = 0, k = 0;
        bool over = false;
        cJSON *d;
        cJSON_ArrayForEach(d, devs) {
            if (k >= ML_MAX) { over = true; break; }
            dev_line(d, mode == 1, rid, lines[k], ML_LEN);
            lcol[k] = dev_color(d);                    // red(alert) > green(running) > dim(idle) > normal
            int lw; h += text_wh(lines[k], 0, &lw);    // natural (unwrapped) size
            if (lw > usable_w) over = true;
            if (lw > maxw) maxw = lw;
            k++;
        }
        if (k == ndev && k > 0 && !over && h <= budget) { nl = k; tier = mode; if (maxw > content_w) content_w = maxw; }
    }
    // Tier 1 — climate glance + counts.
    if (tier < 0) {
        int h = 0, maxw = 0, k = 0;
        bool over = false;
        char cl[ML_LEN]; uint32_t cc;
        if (climate_line(room, cl, sizeof cl, &cc)) {
            snprintf(lines[k], ML_LEN, "%s", cl); lcol[k] = cc;
            int lw; h += text_wh(lines[k], 0, &lw); if (lw > usable_w) over = true; if (lw > maxw) maxw = lw; k++;
        }
        if (ns + na > 0) {
            if (na) snprintf(lines[k], ML_LEN, "%ds %da", ns, na);
            else    snprintf(lines[k], ML_LEN, "%ds", ns);
            lcol[k] = act ? ACT_WALL_COL : C_NORMAL;
            int lw; h += text_wh(lines[k], 0, &lw); if (lw > usable_w) over = true; if (lw > maxw) maxw = lw; k++;
        }
        if (k > 0 && !over && h <= budget) { nl = k; tier = 2; if (maxw > content_w) content_w = maxw; }
    }
    // Tier 2 — counts floor. If even the counts won't fit a sliver room, drop to a bare count BADGE
    // (total device count) so it never overflows the polygon.
    if (tier < 0 && ns + na > 0) {
        if (na) snprintf(lines[0], ML_LEN, "%ds %da", ns, na);
        else    snprintf(lines[0], ML_LEN, "%ds", ns);
        int lw; text_wh(lines[0], 0, &lw);
        if (lw > usable_w) snprintf(lines[0], ML_LEN, "%d", ns + na);   // sliver room -> count badge
        lcol[0] = act ? ACT_WALL_COL : C_NORMAL;
        text_wh(lines[0], 0, &lw); if (lw > content_w) content_w = lw;
        nl = 1; tier = 3;
    }

    // Surplus tier: when content is much smaller than the room, upsize the font (28/20) to fill the
    // space and read at a distance (attic/crawlspace + other big, sparse rooms). Tier selection floored
    // on 14pt so the tier already fits; only grow if the bigger font STILL fits width and height. Never
    // upsize the sliver-room counts floor (tier 3).
    const lv_font_t *font = &lv_font_montserrat_14;
    if (tier != 3) {
        const lv_font_t *cand[2] = { &lv_font_montserrat_28, &lv_font_montserrat_20 };
        for (int ci = 0; ci < 2; ci++) {
            int th, tw; bool fits = true;
            th = text_wh_f(folded, usable_w, cand[ci], &tw);
            if (tw > usable_w) fits = false;
            for (int i = 0; i < nl && fits; i++) {
                int lw; th += text_wh_f(lines[i], 0, cand[ci], &lw);
                if (lw > usable_w) fits = false;
            }
            if (fits && th <= usable_h) { font = cand[ci]; break; }
        }
    }

    // Box sized to the chosen content at the chosen font (bounded by the room bbox).
    int content_w2, content_h = text_wh_f(folded, usable_w, font, &content_w2);
    for (int i = 0; i < nl; i++) { int lw; content_h += text_wh_f(lines[i], 0, font, &lw); if (lw > content_w2) content_w2 = lw; }
    int w = content_w2 + 2 * inset; if (w > bw && bw > 50) w = bw; if (w < 50) w = 50;
    int h = content_h + 2 * inset;
    if (tier != 3 && h > bh && bh > 0) h = bh;         // only the counts floor may overflow
    if (h < 22) h = 22;

    // Position centered on the label anchor, then CLAMP the whole box inside the room px bbox so it
    // can't spill past the walls (the height/width overrun: a box sized to bh centered on an anchor
    // near a short room's edge overflowed into the neighbor). cbx1<=cbx0 -> no clamp.
    int px = cx - w / 2, py = cy - h / 2;
    if (cbx1 > cbx0) { if (px + w > cbx1) px = cbx1 - w; if (px < cbx0) px = cbx0; }
    if (cby1 > cby0) { if (py + h > cby1) py = cby1 - h; if (py < cby0) py = cby0; }

    lv_obj_t *box = lv_obj_create(parent);
    lv_obj_set_size(box, w, h);
    lv_obj_set_pos(box, px, py);
    lv_obj_set_style_bg_color(box, lv_color_hex(0x0b1021), 0);
    lv_obj_set_style_bg_opa(box, strip ? LV_OPA_COVER : 190, 0);   // semi-transparent so walls read through
    lv_obj_set_style_border_width(box, strip ? 2 : 0, 0);
    lv_obj_set_style_border_color(box, lv_color_hex(act ? ACT_WALL_COL : WALL_COL), 0);
    lv_obj_set_style_radius(box, 6, 0);
    lv_obj_set_style_pad_all(box, 3, 0);
    lv_obj_set_style_pad_row(box, 1, 0);
    lv_obj_set_flex_flow(box, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(box, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(box, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(box, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(box, room_clicked_cb, LV_EVENT_CLICKED, (void *)(intptr_t)reg_idx);

    lv_obj_t *t = lv_label_create(box);
    lv_label_set_text(t, folded);
    lv_obj_set_style_text_font(t, font, 0);
    lv_obj_set_style_text_color(t, lv_color_hex(0xffffff), 0);
    lv_label_set_long_mode(t, LV_LABEL_LONG_WRAP);          // multi-line names (e.g. "Dining Nook")
    lv_obj_set_style_text_align(t, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_width(t, lv_pct(100));

    for (int i = 0; i < nl; i++) {
        lv_obj_t *sub = lv_label_create(box);
        lv_label_set_text(sub, lines[i]);
        lv_obj_set_style_text_font(sub, font, 0);
        lv_obj_set_style_text_color(sub, lv_color_hex(lcol[i]), 0);
        lv_obj_set_style_text_align(sub, LV_TEXT_ALIGN_CENTER, 0);
    }
}

// --- house-view spatial fill (placed devices at their normalized spot) -------
// Render a geometried room's devices at their PWA-editor placements instead of one stacked centroid
// box. Adaptive: try full readings (name + metrics); if any would overflow the room bbox or overlap
// a sibling, the WHOLE room degrades to compact markers (primary value only). Room name = a small
// quiet chip at the label anchor. Unplaced devices stack compactly under the name. Everything is
// clamped to the room's px bbox so nothing crosses a wall. Returns false (-> caller uses make_label)
// when the room has no placed device or no usable geometry.
static bool house_room_spatial(lv_obj_t *parent, cJSON *room, cJSON *geo, int reg_idx,
                               const char *name, int mapw, int ph)
{
    cJSON *devs = cJSON_GetObjectItem(room, "devices");
    if (!cJSON_IsArray(devs)) return false;
    const cJSON *ji = cJSON_GetObjectItem(room, "id");
    const char *rid = cJSON_IsString(ji) ? ji->valuestring : "";

    double mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
    room_bounds(geo, &mnx, &mny, &mxx, &mxy);
    if (!(mxx > mnx && mxy > mny)) return false;

    // Room px bbox, clamped to the floor-plan area (chips/name clamp against this).
    int x0 = scr_x(mnx), y0 = scr_y(mny), x1 = scr_x(mxx), y1 = scr_y(mxy);
    if (x0 < PAD) x0 = PAD;
    if (x1 > mapw - PAD) x1 = mapw - PAD;
    if (y0 < PAD) y0 = PAD;
    if (y1 > ph - PAD) y1 = ph - PAD;

    // Gather placed chips (full-reading geometry) for the fit test.
    struct { int cx, cy, w, h; cJSON *dev; } ch[MAX_DEVS];
    int nc = 0;
    cJSON *d;
    cJSON_ArrayForEach(d, devs) {
        if (nc >= MAX_DEVS) break;
        cJSON *pl = cJSON_GetObjectItem(d, "placement");
        cJSON *jx = pl ? cJSON_GetObjectItem(pl, "x") : NULL;
        cJSON *jy = pl ? cJSON_GetObjectItem(pl, "y") : NULL;
        if (!(cJSON_IsNumber(jx) && cJSON_IsNumber(jy))) continue;   // unplaced -> handled below
        double hx = mnx + jx->valuedouble * (mxx - mnx);
        double hy = mny + jy->valuedouble * (mxy - mny);
        char line[ML_LEN]; dev_line(d, false, rid, line, sizeof line);
        int w, h = text_wh(line, 0, &w);
        ch[nc].cx = scr_x(hx); ch[nc].cy = scr_y(hy); ch[nc].w = w + 10; ch[nc].h = h + 6; ch[nc].dev = d;
        nc++;
    }
    if (nc == 0) return false;                                       // nothing placed -> centroid box

    // Decide full vs compact for the whole room: full only if every chip fits the bbox and no two
    // overlap. (Chip boxes measured at their natural on-screen centers, before the per-chip clamp.)
    bool full = true;
    for (int i = 0; i < nc && full; i++) {
        int ax0 = ch[i].cx - ch[i].w / 2, ax1 = ax0 + ch[i].w;
        int ay0 = ch[i].cy - ch[i].h / 2, ay1 = ay0 + ch[i].h;
        if (ax0 < x0 || ax1 > x1 || ay0 < y0 || ay1 > y1) full = false;
        for (int j = 0; j < i && full; j++) {
            int bx0 = ch[j].cx - ch[j].w / 2, bx1 = bx0 + ch[j].w;
            int by0 = ch[j].cy - ch[j].h / 2, by1 = by0 + ch[j].h;
            if (ax0 < bx1 && bx0 < ax1 && ay0 < by1 && by0 < ay1) full = false;   // overlap
        }
    }

    // Room name — small chip at the label anchor (fallback: bbox top-center), clamped, clickable.
    double lx, ly; int nx, ny;
    if (room_label(geo, &lx, &ly)) { nx = scr_x(lx); ny = scr_y(ly); }
    else { nx = (x0 + x1) / 2; ny = y0 + 12; }
    char folded[40]; ascii_fold(name, folded, sizeof folded);
    house_chip(parent, folded, 0xcfe0ff, true, nx, ny, reg_idx, x0, y0, x1, y1);

    // Placed devices at their spots.
    for (int i = 0; i < nc; i++) {
        char txt[ML_LEN];
        if (full) dev_line(ch[i].dev, false, rid, txt, sizeof txt);
        else      dev_compact(ch[i].dev, txt, sizeof txt);
        house_chip(parent, txt, dev_color(ch[i].dev), false, ch[i].cx, ch[i].cy, reg_idx, x0, y0, x1, y1);
    }

    // Unplaced devices (this room has geometry + some placed): compact stack under the name.
    int uy = ny + 18;
    cJSON_ArrayForEach(d, devs) {
        cJSON *pl = cJSON_GetObjectItem(d, "placement");
        cJSON *jx = pl ? cJSON_GetObjectItem(pl, "x") : NULL;
        cJSON *jy = pl ? cJSON_GetObjectItem(pl, "y") : NULL;
        if (cJSON_IsNumber(jx) && cJSON_IsNumber(jy)) continue;      // already placed
        char txt[ML_LEN]; dev_compact(d, txt, sizeof txt);
        house_chip(parent, txt, dev_color(d), false, nx, uy, reg_idx, x0, y0, x1, y1);
        uy += 18;
    }

    ESP_LOGI(TAG, "spatial %s: %d placed (%s)", rid, nc, full ? "full" : "compact");
    return true;
}

// --- render ----------------------------------------------------------------
// Two persistent layers under `parent`: s_wall_c (static vector walls — never change) and s_box_c
// (live value labels — rebuilt each refresh). The walls span the whole floor plan, so tearing them
// down + redrawing every 10 s invalidated the WHOLE screen -> the flicker. Now the walls are built
// once and only the (much smaller) box layer is cleared+rebuilt per cycle, keyed on a structural
// signature so a device/geometry/size change still triggers a full rebuild. (panel map-flicker fix)
static lv_obj_t *s_wall_c, *s_box_c, *s_map_parent;
static unsigned  s_shape_sig;

// FNV-1a over the STRUCTURE only (room ids + device ids + parent px) — NOT the values, so it is stable
// across value drift and flips only when the set of things shown / geometry-scale / size changes.
static unsigned map_shape_sig(cJSON *rooms, int pw, int ph)
{
    unsigned h = 2166136261u;
    #define MIX(b) (h ^= (unsigned char)(b), h *= 16777619u)
    cJSON *r;
    cJSON_ArrayForEach(r, rooms) {
        const cJSON *id = cJSON_GetObjectItem(r, "id");
        if (cJSON_IsString(id)) for (const char *s = id->valuestring; *s; s++) MIX(*s);
        MIX('|');
        cJSON *devs = cJSON_GetObjectItem(r, "devices");
        cJSON *d;
        cJSON_ArrayForEach(d, devs) {
            const cJSON *did = cJSON_GetObjectItem(d, "device_id");
            if (cJSON_IsString(did)) for (const char *s = did->valuestring; *s; s++) MIX(*s);
            MIX(',');
        }
    }
    MIX(pw & 0xff); MIX((pw >> 8) & 0xff); MIX(ph & 0xff); MIX((ph >> 8) & 0xff);
    #undef MIX
    return h;
}

static lv_obj_t *map_make_layer(lv_obj_t *parent, int pw, int ph)
{
    lv_obj_t *c = lv_obj_create(parent);
    lv_obj_set_size(c, pw, ph);
    lv_obj_set_pos(c, 0, 0);
    lv_obj_set_style_bg_opa(c, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(c, 0, 0);
    lv_obj_set_style_pad_all(c, 0, 0);
    lv_obj_set_style_radius(c, 0, 0);
    lv_obj_set_layout(c, LV_LAYOUT_NONE);
    lv_obj_clear_flag(c, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_clear_flag(c, LV_OBJ_FLAG_CLICKABLE);   // pass taps through to the child boxes
    return c;
}

void ui_map_render(cJSON *root, lv_obj_t *parent, ui_map_room_cb cb, bool nav)
{
    s_cb = cb;
    s_nreg = 0;
    s_xoff = 0; s_yoff = 0;                    // house map: no centering offset
    lv_obj_set_layout(parent, LV_LAYOUT_NONE);

    cJSON *rooms = cJSON_GetObjectItem(root, "rooms");
    if (!cJSON_IsArray(rooms)) { ESP_LOGW(TAG, "no rooms[]"); return; }

    double mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
    cJSON *r;
    cJSON_ArrayForEach(r, rooms) room_bounds(cJSON_GetObjectItem(r, "geometry"), &mnx, &mny, &mxx, &mxy);
    bool have_space = (mxx > mnx && mxy > mny);

    lv_obj_update_layout(parent);              // resolve flex_grow height before measuring
    int pw = lv_obj_get_width(parent), ph = lv_obj_get_height(parent);
    if (pw <= 0) pw = 780;
    if (ph <= 0) ph = 1140;
    int mapw = pw - COL_W;                       // floor plan = left area; right column = monolithic rooms
    s_mnx = mnx; s_mny = mny;
    double sx = have_space ? (double)(mapw - 2 * PAD) / (mxx - mnx) : 1;
    double sy = have_space ? (double)(ph - 2 * PAD) / (mxy - mny) : 1;   // full screen height now
    s_scale = sx < sy ? sx : sy;                 // uniform, preserve aspect

    // Rebuild the static wall layer only when the structure changed (else reuse it, redraw-free);
    // always clear+rebuild the value-box layer.
    unsigned sig = map_shape_sig(rooms, pw, ph);
    // nav (view entry) forces rebuild: another view may have lv_obj_clean'd `parent`, freeing our layers,
    // so s_box_c could be stale — the rebuild branch never derefs it (it lv_obj_clean's parent + recreates).
    bool rebuild_walls = (nav || !s_box_c || parent != s_map_parent || sig != s_shape_sig);
    if (rebuild_walls) {
        lv_obj_clean(parent);
        s_wall_c = map_make_layer(parent, pw, ph);
        s_box_c  = map_make_layer(parent, pw, ph);   // created after -> drawn on top of the walls
        s_map_parent = parent; s_shape_sig = sig;
        s_nring = 0;
        // pass 1: walls (into the persistent wall layer)
        if (have_space) {
            cJSON_ArrayForEach(r, rooms) {
                cJSON *geo = cJSON_GetObjectItem(r, "geometry");
                if (!cJSON_IsObject(geo)) continue;
                cJSON *counts = cJSON_GetObjectItem(r, "counts");
                cJSON *ca = cJSON_IsObject(counts) ? cJSON_GetObjectItem(counts, "actuators") : NULL;
                uint32_t col = (cJSON_IsNumber(ca) && ca->valueint > 0) ? ACT_WALL_COL : WALL_COL;
                cJSON *poly = cJSON_GetObjectItem(geo, "poly");
                if (cJSON_IsArray(poly)) draw_ring(poly, s_wall_c, col);
                cJSON *polys = cJSON_GetObjectItem(geo, "polys");
                if (cJSON_IsArray(polys)) {
                    cJSON *ring;
                    cJSON_ArrayForEach(ring, polys) draw_ring(ring, s_wall_c, col);
                }
            }
        }
    } else {
        lv_obj_clean(s_box_c);                   // walls persist; only the value layer redraws
    }
    parent = s_box_c;                            // pass 2 (labels) target the value layer

    // pass 2: labels (placed rooms at centroids; monolithic/geometry-less-with-devices -> right column)
    int col_y = PAD + 30;
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
        const cJSON *rname = jn;
        if (have_space && room_label(geo, &lx, &ly)) {
            // Prefer spatial placement (PWA-editor spots). Falls back to the centroid tier box for a
            // room with no placed device (house_room_spatial returns false).
            const char *nm = cJSON_IsString(rname) ? rname->valuestring : s_reg[idx].id;
            if (!house_room_spatial(parent, r, geo, idx, nm, mapw, ph)) {
                int x = scr_x(lx), y = scr_y(ly);
                int bw = LBL_W, bh = LBL_H;
                room_screen_bbox(geo, &bw, &bh);
                // room px bbox -> clamp rect (keeps the box inside the walls; fixes the overrun)
                double rmnx = 1e9, rmny = 1e9, rmxx = -1e9, rmxy = -1e9;
                room_bounds(geo, &rmnx, &rmny, &rmxx, &rmxy);
                int cbx0 = scr_x(rmnx), cby0 = scr_y(rmny), cbx1 = scr_x(rmxx), cby1 = scr_y(rmxy);
                if (cbx0 < PAD) cbx0 = PAD;
                if (cbx1 > mapw - PAD) cbx1 = mapw - PAD;
                if (cby0 < PAD) cby0 = PAD;
                if (cby1 > ph - PAD) cby1 = ph - PAD;
                if (x < PAD + 26) x = PAD + 26;
                if (x > mapw - PAD - 26) x = mapw - PAD - 26;
                if (y < PAD + 14) y = PAD + 14;
                if (y > ph - PAD - 14) y = ph - PAD - 14;
                make_label(parent, r, idx, x, y, act, false, bw, bh, cbx0, cby0, cbx1, cby1);
            }
            s_nreg++;
        } else if (ns + na > 0) {
            if (col_y + 30 > ph - PAD) continue;    // right column full
            int cbx0 = pw - COL_W, cby0 = col_y - 29, cbx1 = pw - 6, cby1 = col_y + 29;
            make_label(parent, r, idx, pw - COL_W / 2, col_y, act, true, COL_W - 12, 58,
                       cbx0, cby0, cbx1, cby1);
            col_y += 58 + 12;
            s_nreg++;
        }
    }
    ESP_LOGI(TAG, "rendered floor plan: %d rooms, %d wall rings (space=%d)", s_nreg, s_nring, have_space);
}

// --- spatial room-zoom (arc 3) ---------------------------------------------
// One device callout: its room-fill line (name + metrics or actuator action), colored by state,
// clickable. Registered in s_dreg so the tap survives the cJSON free. `cx,cy` = center (px).
static void device_chip(lv_obj_t *parent, cJSON *dev, const char *room_id, int cx, int cy)
{
    if (s_ndreg >= MAX_DEVS) return;
    const cJSON *jid = cJSON_GetObjectItem(dev, "device_id");
    const cJSON *jn = cJSON_GetObjectItem(dev, "name");
    int idx = s_ndreg++;
    strncpy(s_dreg[idx].id, cJSON_IsString(jid) ? jid->valuestring : "?", sizeof s_dreg[idx].id - 1);
    s_dreg[idx].id[sizeof s_dreg[idx].id - 1] = 0;
    strncpy(s_dreg[idx].name, cJSON_IsString(jn) ? jn->valuestring : s_dreg[idx].id, sizeof s_dreg[idx].name - 1);
    s_dreg[idx].name[sizeof s_dreg[idx].name - 1] = 0;

    char line[ML_LEN];
    dev_line(dev, false, room_id, line, sizeof line);
    uint32_t col = dev_color(dev);
    int w, hh = text_wh(line, 0, &w);

    lv_obj_t *box = lv_obj_create(parent);
    lv_obj_set_size(box, w + 14, hh + 10);
    lv_obj_set_pos(box, cx - (w + 14) / 2, cy - (hh + 10) / 2);
    lv_obj_set_style_bg_color(box, lv_color_hex(0x0b1021), 0);
    lv_obj_set_style_bg_opa(box, 220, 0);
    lv_obj_set_style_border_width(box, 2, 0);
    lv_obj_set_style_border_color(box, lv_color_hex(col), 0);
    lv_obj_set_style_radius(box, 6, 0);
    lv_obj_set_style_pad_all(box, 3, 0);
    lv_obj_clear_flag(box, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(box, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(box, device_clicked_cb, LV_EVENT_CLICKED, (void *)(intptr_t)idx);

    lv_obj_t *t = lv_label_create(box);
    lv_label_set_text(t, line);
    lv_obj_set_style_text_font(t, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(t, lv_color_hex(col), 0);
    lv_obj_set_style_text_align(t, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_center(t);
}

bool ui_map_render_room(cJSON *root, const char *room_id, lv_obj_t *parent, ui_map_device_cb cb)
{
    s_dcb = cb;
    s_ndreg = 0;
    s_nring = 0;

    cJSON *rooms = cJSON_GetObjectItem(root, "rooms");
    if (!cJSON_IsArray(rooms) || !room_id) return false;
    cJSON *r, *room = NULL;
    cJSON_ArrayForEach(r, rooms) {
        cJSON *id = cJSON_GetObjectItem(r, "id");
        if (cJSON_IsString(id) && strcmp(id->valuestring, room_id) == 0) { room = r; break; }
    }
    if (!room) return false;
    cJSON *geo = cJSON_GetObjectItem(room, "geometry");
    if (!cJSON_IsObject(geo)) return false;                 // no polygon -> caller uses the tile grid

    double mnx = 1e9, mny = 1e9, mxx = -1e9, mxy = -1e9;
    room_bounds(geo, &mnx, &mny, &mxx, &mxy);
    if (!(mxx > mnx && mxy > mny)) return false;

    lv_obj_clean(parent);
    lv_obj_set_layout(parent, LV_LAYOUT_NONE);
    lv_obj_update_layout(parent);
    int pw = lv_obj_get_width(parent), ph = lv_obj_get_height(parent);
    if (pw <= 0) pw = 780;
    if (ph <= 0) ph = 1140;
    const int strip_h = 132;                                // bottom strip for unplaced devices
    int maph = ph - strip_h;

    s_mnx = mnx; s_mny = mny;
    double sx = (double)(pw - 2 * PAD) / (mxx - mnx);
    double sy = (double)(maph - 2 * PAD) / (mxy - mny);
    s_scale = sx < sy ? sx : sy;                            // uniform, preserve aspect
    int roomw = (int)((mxx - mnx) * s_scale), roomh = (int)((mxy - mny) * s_scale);
    s_xoff = (pw - 2 * PAD - roomw) / 2;                    // center the room in the top region
    s_yoff = (maph - 2 * PAD - roomh) / 2;

    // walls
    cJSON *poly = cJSON_GetObjectItem(geo, "poly");
    if (cJSON_IsArray(poly)) draw_ring(poly, parent, WALL_COL);
    cJSON *polys = cJSON_GetObjectItem(geo, "polys");
    if (cJSON_IsArray(polys)) {
        cJSON *ring;
        cJSON_ArrayForEach(ring, polys) draw_ring(ring, parent, WALL_COL);
    }

    // devices: placed (non-null normalized x/y) on the diagram, unplaced -> bottom fallback strip
    cJSON *devs = cJSON_GetObjectItem(room, "devices"), *d;
    int strip_x = PAD + 66, strip_y = maph + 30, nplaced = 0, nstrip = 0;
    cJSON_ArrayForEach(d, devs) {
        cJSON *pl = cJSON_GetObjectItem(d, "placement");
        cJSON *px = pl ? cJSON_GetObjectItem(pl, "x") : NULL;
        cJSON *py = pl ? cJSON_GetObjectItem(pl, "y") : NULL;
        if (cJSON_IsNumber(px) && cJSON_IsNumber(py)) {     // normalized 0..1 of the room bbox
            double hx = mnx + px->valuedouble * (mxx - mnx);
            double hy = mny + py->valuedouble * (mxy - mny);
            device_chip(parent, d, room_id, scr_x(hx), scr_y(hy));
            nplaced++;
        } else {
            if (strip_x > pw - 66) { strip_x = PAD + 66; strip_y += 46; }   // wrap to a 2nd strip row
            if (strip_y < ph - 12) { device_chip(parent, d, room_id, strip_x, strip_y); strip_x += 138; nstrip++; }
        }
    }
    ESP_LOGI(TAG, "room-zoom %s: %d placed, %d in strip, %d rings", room_id, nplaced, nstrip, s_nring);
    return true;
}
