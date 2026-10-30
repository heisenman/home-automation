// Sensors view (see ui_sensors.h). Rebuilt on every refresh (10 s) from /api/v1/panel/sensors; the
// scroll position is carried across the rebuild. Two modes: the room grid (default) and the per-sensor
// Edit list where sensors are hidden / un-hidden. Fonts compiled: montserrat 14/20/28 only.
#include "ui/ui_sensors.h"
#include "ui/ui_format.h"   // ascii_fold, aq_band_color, disp_val
#include "ui/ui_pick.h"     // Other column: which extra reading to graph
#include <stdio.h>
#include <string.h>
#include <strings.h>   // strcasecmp
#include "esp_log.h"
#include "nvs.h"

static const char *TAG = "ui.sensors";

#define NVS_NS      "senslist"
#define NVS_KEY     "hidden"
#define NVS_ORDER   "rooms"         // this panel's room order: comma-separated room slugs
#define MAX_HIDDEN  64
#define ID_LEN      40
#define STALE_S     900            // older than this -> values drawn dim (sensor quiet / offline)

// column widths (800 px portrait panel, 10 px screen inset): name | temp | hum | dew | air | notable | button
// (the room grid uses the same columns, with the button's width going to the notable column)
#define W_NAME  230
#define W_ROOM  170            // room grid's name column: narrower, the Other column takes the 60 px
#define W_TEMP   88
#define W_HUM    66
#define W_DEW    88
#define W_AIR   112
#define W_BTN    56

#define C_ROW     0x111834
#define C_TEXT    0xffffff
#define C_DIM     0x64748b
#define C_HEAD    0x8fb4ff
#define C_BTN     0x16204a
#define C_EDGE    0x2f7e7a

static char s_hidden[MAX_HIDDEN][ID_LEN];
static int s_nhidden;
static bool s_edit;                 // Edit mode: per-sensor list, hidden rows shown dimmed, hide/un-hide buttons

// Room-grid sort, from tapping a column heading (this session only — a reboot is back to the panel's order).
// Room: A-Z, then Z-A. Temp/Hum/Dew/Air: high-to-low, then low-to-high (Air = band 5 Excellent .. 1 Very
// Poor, so best air first). A room's stacked values sort by their average; a room with no value in that
// column always goes last. "Your order" clears it.
enum { SORT_NONE, SORT_ROOM, SORT_TEMP, SORT_HUM, SORT_DEW, SORT_AIR };
static int s_sort_col = SORT_NONE;
static bool s_sort_asc;
static void (*s_changed)(void);
static void (*s_open_graph)(const char *device_id, const char *metric);

// The last render's sensor rows, kept until the next render: a grid value's tap user_data points at
// its row here. Freed only after lv_obj_clean() has deleted the labels that point into it (both under the
// LVGL lock, like the tap itself), so a tap can never see a freed row.
static cJSON *s_rows;

// The open Other-column menu's choices, copied out of s_rows (a refresh may free that while it's open).
static char s_pick_id[ID_LEN];
static char s_pick_key[UI_PICK_MAX][24];

// per-row button payloads (device ids) — stable storage for the click user_data across one render
#define MAX_ROWS 96
static char s_row_id[MAX_ROWS][ID_LEN];

#define MAX_PER_ROOM 12             // sensors stacked in one room row (more are dropped from the grid)
#define MAX_ROOMS    32

// Room order. s_order = this panel's saved choice (NVS); s_shown = the order the last render used — the
// saved rooms that still exist, in their saved order, then any others in server (areas.yaml) order. The
// arrows move a room within s_shown and save the whole of it, so a new room lands at the end until moved.
static char s_order[MAX_ROOMS][ID_LEN];
static int s_norder;
static char s_shown[MAX_ROOMS][ID_LEN];
static int s_nshown;

// ── hidden set (NVS: comma-separated device ids) ─────────────────────────────
static int find_hidden(const char *id)
{
    for (int i = 0; i < s_nhidden; i++) if (strcmp(s_hidden[i], id) == 0) return i;
    return -1;
}

// comma-separated list <-> fixed array (NVS strings for the hidden set and the room order)
static void save_list(const char *key, char (*ids)[ID_LEN], int n)
{
    char buf[MAX_HIDDEN * ID_LEN];
    int len = 0;
    buf[0] = 0;
    for (int i = 0; i < n && len < (int)sizeof buf - ID_LEN - 1; i++)
        len += snprintf(buf + len, sizeof buf - len, "%s%s", i ? "," : "", ids[i]);
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) { ESP_LOGW(TAG, "nvs open failed"); return; }
    if (n) nvs_set_str(h, key, buf); else nvs_erase_key(h, key);
    nvs_commit(h);
    nvs_close(h);
}

static int load_list(nvs_handle_t h, const char *key, char (*ids)[ID_LEN], int max)
{
    char buf[MAX_HIDDEN * ID_LEN];
    size_t len = sizeof buf;
    int n = 0;
    if (nvs_get_str(h, key, buf, &len) != ESP_OK) return 0;
    char *save = NULL;
    for (char *t = strtok_r(buf, ",", &save); t && n < max; t = strtok_r(NULL, ",", &save))
        snprintf(ids[n++], ID_LEN, "%s", t);
    return n;
}

static void save_hidden(void) { save_list(NVS_KEY, s_hidden, s_nhidden); }

void ui_sensors_init(void)
{
    s_nhidden = s_norder = 0;
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return;
    s_nhidden = load_list(h, NVS_KEY, s_hidden, MAX_HIDDEN);
    s_norder = load_list(h, NVS_ORDER, s_order, MAX_ROOMS);
    nvs_close(h);
    ESP_LOGI(TAG, "%d hidden sensor(s), %d room(s) ordered", s_nhidden, s_norder);
}

// ── clicks (LVGL lock held) ──────────────────────────────────────────────────
static void on_hide_toggle(lv_event_t *e)
{
    const char *id = lv_event_get_user_data(e);
    int i = find_hidden(id);
    if (i >= 0) {                                       // un-hide
        s_nhidden--;
        if (i != s_nhidden) memcpy(s_hidden[i], s_hidden[s_nhidden], ID_LEN);
    } else if (s_nhidden < MAX_HIDDEN) {                // hide
        snprintf(s_hidden[s_nhidden++], ID_LEN, "%s", id);
    }
    save_hidden();
    ESP_LOGI(TAG, "%s %s (%d hidden)", i >= 0 ? "unhide" : "hide", id, s_nhidden);
    if (s_changed) s_changed();
}

// room arrows (Edit mode): user_data = the room's index in s_shown; swap with the neighbour and save
static void move_room(int i, int to)
{
    if (i < 0 || to < 0 || i >= s_nshown || to >= s_nshown) return;
    char tmp[ID_LEN];
    memcpy(tmp, s_shown[i], ID_LEN);
    memcpy(s_shown[i], s_shown[to], ID_LEN);
    memcpy(s_shown[to], tmp, ID_LEN);
    memcpy(s_order, s_shown, sizeof s_order);
    s_norder = s_nshown;
    save_list(NVS_ORDER, s_order, s_norder);
    ESP_LOGI(TAG, "room %s -> position %d", s_shown[to], to + 1);
    if (s_changed) s_changed();
}
static void on_room_up(lv_event_t *e)   { int i = (int)(intptr_t)lv_event_get_user_data(e); move_room(i, i - 1); }
static void on_room_down(lv_event_t *e) { int i = (int)(intptr_t)lv_event_get_user_data(e); move_room(i, i + 1); }

static void on_sort(lv_event_t *e)
{
    int c = (int)(intptr_t)lv_event_get_user_data(e);
    if (c == SORT_NONE || c != s_sort_col) { s_sort_col = c; s_sort_asc = (c == SORT_ROOM); }
    else s_sort_asc = !s_sort_asc;
    if (s_changed) s_changed();
}

static void on_edit(lv_event_t *e)
{
    (void)e;
    s_edit = !s_edit;
    if (s_changed) s_changed();
}

// grid taps: user_data = the sensor's row in s_rows
static const char *row_id(cJSON *e)
{
    cJSON *j = cJSON_GetObjectItem(e, "device_id");
    return cJSON_IsString(j) ? j->valuestring : NULL;
}

static void open_graph(cJSON *e, const char *metric)
{
    const char *id = row_id(e);
    if (!id || !s_open_graph) return;
    ESP_LOGI(TAG, "tap %s/%s -> graph", id, metric);
    s_open_graph(id, metric);
}

static void on_tap_temp(lv_event_t *e) { open_graph(lv_event_get_user_data(e), "temperature_c"); }
static void on_tap_hum(lv_event_t *e)  { open_graph(lv_event_get_user_data(e), "humidity_pct"); }
static void on_tap_dew(lv_event_t *e)  { open_graph(lv_event_get_user_data(e), "dewpoint_c"); }
static void on_tap_air(lv_event_t *e)  { open_graph(lv_event_get_user_data(e), "air_quality"); }

static void on_pick(int i)
{
    if (i >= 0 && i < UI_PICK_MAX && s_open_graph) {
        ESP_LOGI(TAG, "picked %s/%s -> graph", s_pick_id, s_pick_key[i]);
        s_open_graph(s_pick_id, s_pick_key[i]);
    }
}

// Other column: one notable reading -> its graph; several -> a menu of them (labels as the server wrote them)
static void on_tap_notable(lv_event_t *ev)
{
    cJSON *e = lv_event_get_user_data(ev), *it;
    const char *id = row_id(e);
    if (!id) return;
    char labels[UI_PICK_MAX][24];
    const char *lp[UI_PICK_MAX];
    int n = 0;
    cJSON_ArrayForEach(it, cJSON_GetObjectItem(e, "notable")) {
        cJSON *k = cJSON_GetObjectItem(it, "key"), *lb = cJSON_GetObjectItem(it, "label");
        if (!cJSON_IsString(k) || n >= UI_PICK_MAX) continue;
        snprintf(s_pick_key[n], sizeof s_pick_key[n], "%s", k->valuestring);
        ascii_fold(cJSON_IsString(lb) ? lb->valuestring : k->valuestring, labels[n], sizeof labels[n]);
        lp[n] = labels[n];
        n++;
    }
    if (n == 0) return;
    snprintf(s_pick_id, sizeof s_pick_id, "%s", id);
    if (n == 1) { on_pick(0); return; }
    cJSON *jnm = cJSON_GetObjectItem(e, "name");
    ui_pick_open(cJSON_IsString(jnm) ? jnm->valuestring : id, lp, n, on_pick);
}

// make a grid value line tappable (single tap: LVGL's DOUBLE_CLICKED never reached these labels on the
// panel, v122 — the debug log showed no handler call — while CLICKED works on the sort headers)
static void tap_target(lv_obj_t *l, lv_event_cb_t cb, cJSON *row)
{
    lv_obj_add_flag(l, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(l, cb, LV_EVENT_CLICKED, row);
}

// ── widgets ──────────────────────────────────────────────────────────────────
static lv_obj_t *row_box(lv_obj_t *parent, bool card)
{
    lv_obj_t *r = lv_obj_create(parent);
    lv_obj_set_width(r, lv_pct(100));
    lv_obj_set_height(r, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_color(r, lv_color_hex(C_ROW), 0);
    lv_obj_set_style_bg_opa(r, card ? LV_OPA_COVER : 0, 0);
    lv_obj_set_style_border_width(r, 0, 0);
    lv_obj_set_style_radius(r, 8, 0);
    lv_obj_set_style_pad_all(r, card ? 6 : 0, 0);
    lv_obj_set_style_pad_left(r, 8, 0);
    lv_obj_set_style_pad_column(r, 6, 0);
    lv_obj_set_flex_flow(r, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(r, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(r, LV_OBJ_FLAG_SCROLLABLE);
    return r;
}

static lv_obj_t *cell(lv_obj_t *row, const char *txt, int w, const lv_font_t *font, uint32_t col, bool right)
{
    lv_obj_t *l = lv_label_create(row);
    lv_label_set_text(l, txt);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(col), 0);
    lv_label_set_long_mode(l, LV_LABEL_LONG_DOT);
    if (w > 0) lv_obj_set_width(l, w); else { lv_obj_set_width(l, 1); lv_obj_set_flex_grow(l, 1); }
    if (right) lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_RIGHT, 0);
    return l;
}

static lv_obj_t *button(lv_obj_t *parent, const char *txt, int w, lv_event_cb_t cb, void *ud)
{
    lv_obj_t *b = lv_obj_create(parent);
    lv_obj_set_size(b, w, 40);
    lv_obj_set_style_bg_color(b, lv_color_hex(C_BTN), 0);
    lv_obj_set_style_border_color(b, lv_color_hex(C_EDGE), 0);
    lv_obj_set_style_border_width(b, 2, 0);
    lv_obj_set_style_radius(b, 8, 0);
    lv_obj_set_style_pad_all(b, 0, 0);
    lv_obj_clear_flag(b, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, ud);
    lv_obj_t *l = lv_label_create(b);
    lv_label_set_text(l, txt);
    lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(C_TEXT), 0);
    lv_obj_center(l);
    return b;
}

static void fmt_temp(cJSON *j, char *out, size_t cap)
{
    if (cJSON_IsNumber(j)) snprintf(out, cap, "%.1fF", disp_val("C", j->valuedouble));
    else snprintf(out, cap, "-");
}

// ── room grid: one row per room, a sensor's values stacked one above the other ──
// A column cell is a vertical box with one label per sensor that HAS that value, so two thermometers in
// a room read as two temperatures, one above the other, while a room with one shows a single line.
static lv_obj_t *col_box(lv_obj_t *row, int w)
{
    lv_obj_t *c = lv_obj_create(row);
    lv_obj_set_height(c, LV_SIZE_CONTENT);
    if (w > 0) lv_obj_set_width(c, w); else { lv_obj_set_width(c, 1); lv_obj_set_flex_grow(c, 1); }
    lv_obj_set_style_bg_opa(c, 0, 0);
    lv_obj_set_style_border_width(c, 0, 0);
    lv_obj_set_style_pad_all(c, 0, 0);
    lv_obj_set_style_pad_row(c, 2, 0);
    lv_obj_set_flex_flow(c, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(c, LV_OBJ_FLAG_SCROLLABLE);
    return c;
}

static lv_obj_t *line(lv_obj_t *col, const char *txt, const lv_font_t *font, uint32_t color, bool right)
{
    lv_obj_t *l = lv_label_create(col);
    lv_label_set_text(l, txt);
    lv_obj_set_width(l, lv_pct(100));
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(color), 0);
    lv_label_set_long_mode(l, LV_LABEL_LONG_DOT);
    if (right) lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_RIGHT, 0);
    return l;
}

// notable tail: "Radon 48Bq  Pressure 1010hPa" (server-authored labels/units); "" when the sensor has none
static void fmt_notable(cJSON *e, char *note, size_t cap)
{
    int n = 0;
    note[0] = 0;
    cJSON *jn = cJSON_GetObjectItem(e, "notable"), *it;
    cJSON_ArrayForEach(it, jn) {
        cJSON *lb = cJSON_GetObjectItem(it, "label"), *v = cJSON_GetObjectItem(it, "value");
        cJSON *u = cJSON_GetObjectItem(it, "unit");
        if (!cJSON_IsString(lb) || !cJSON_IsNumber(v) || n >= (int)cap - 40) continue;
        char lf[24], uf[12];
        ascii_fold(lb->valuestring, lf, sizeof lf);
        ascii_fold(cJSON_IsString(u) ? u->valuestring : "", uf, sizeof uf);
        n += snprintf(note + n, cap - n, "%s%s %g%s", n ? "  " : "", lf, v->valuedouble, uf);
    }
}

static bool is_stale(cJSON *e)
{
    cJSON *jage = cJSON_GetObjectItem(e, "age_s");
    return cJSON_IsNumber(jage) && jage->valuedouble > STALE_S;
}

// a tappable column heading: "Temp" + the arrow when it's the sort column (up = ascending)
static void sort_head(lv_obj_t *hdr, const char *txt, int w, int col, bool right)
{
    char t[24];
    snprintf(t, sizeof t, "%s%s", txt, s_sort_col != col ? "" : s_sort_asc ? " " LV_SYMBOL_UP : " " LV_SYMBOL_DOWN);
    lv_obj_t *l = cell(hdr, t, w, &lv_font_montserrat_14, s_sort_col == col ? C_TEXT : C_HEAD, right);
    lv_obj_add_flag(l, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_ext_click_area(l, 14);                          // a 14 px label is a small finger target
    lv_obj_add_event_cb(l, on_sort, LV_EVENT_CLICKED, (void *)(intptr_t)col);
}

// one grid row's sort key: room name, or the average of the room's values for the sort column
typedef struct { const char *slug, *name; cJSON *ss[MAX_PER_ROOM]; int n; double key; bool has; } room_ent_t;
static room_ent_t s_ent[MAX_ROOMS];

static void sort_key(room_ent_t *r)
{
    const char *k = s_sort_col == SORT_TEMP ? "temperature_c" : s_sort_col == SORT_HUM ? "humidity_pct"
                  : s_sort_col == SORT_DEW ? "dewpoint_c" : s_sort_col == SORT_AIR ? "air_band" : NULL;
    double sum = 0;
    int cnt = 0;
    for (int i = 0; k && i < r->n; i++) {
        cJSON *j = cJSON_GetObjectItem(r->ss[i], k);
        if (cJSON_IsNumber(j)) { sum += j->valuedouble; cnt++; }
    }
    r->has = cnt > 0;
    r->key = cnt ? sum / cnt : 0;
}

static int sort_cmp(const room_ent_t *a, const room_ent_t *b)        // <0: a goes first
{
    if (s_sort_col == SORT_ROOM) {
        int c = strcasecmp(a->name, b->name);
        return s_sort_asc ? c : -c;
    }
    if (a->has != b->has) return a->has ? -1 : 1;                  // no value -> last, either direction
    if (!a->has || a->key == b->key) return 0;
    return (a->key < b->key) == s_sort_asc ? -1 : 1;
}

// one room's row from its (visible) sensors, in server order
static void room_row(lv_obj_t *parent, const char *room_name, cJSON **ss, int n)
{
    lv_obj_t *r = row_box(parent, true);
    lv_obj_set_flex_align(r, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);  // top-align stacks
    char buf[160];
    ascii_fold(room_name, buf, sizeof buf);
    line(col_box(r, W_ROOM), buf, &lv_font_montserrat_20, C_TEXT, false);

    lv_obj_t *ct = col_box(r, W_TEMP), *ch = col_box(r, W_HUM), *cd = col_box(r, W_DEW);
    lv_obj_t *ca = col_box(r, W_AIR), *cn = col_box(r, 0);
    int kt = 0, kh = 0, kd = 0, ka = 0;
    for (int i = 0; i < n; i++) {
        cJSON *e = ss[i], *j;
        uint32_t vc = is_stale(e) ? C_DIM : C_TEXT;
        if (cJSON_IsNumber(j = cJSON_GetObjectItem(e, "temperature_c"))) {
            fmt_temp(j, buf, sizeof buf); tap_target(line(ct, buf, &lv_font_montserrat_20, vc, true), on_tap_temp, e); kt++;
        }
        if (cJSON_IsNumber(j = cJSON_GetObjectItem(e, "humidity_pct"))) {
            snprintf(buf, sizeof buf, "%d%%", j->valueint);
            tap_target(line(ch, buf, &lv_font_montserrat_20, vc, true), on_tap_hum, e); kh++;
        }
        if (cJSON_IsNumber(j = cJSON_GetObjectItem(e, "dewpoint_c"))) {
            fmt_temp(j, buf, sizeof buf); tap_target(line(cd, buf, &lv_font_montserrat_20, vc, true), on_tap_dew, e); kd++;
        }
        cJSON *jb = cJSON_GetObjectItem(e, "air_band"), *jbl = cJSON_GetObjectItem(e, "air_band_label");
        if (cJSON_IsNumber(jb) && cJSON_IsString(jbl)) {
            ascii_fold(jbl->valuestring, buf, sizeof buf);
            lv_obj_t *l = line(ca, buf, &lv_font_montserrat_20, vc == C_DIM ? C_DIM : aq_band_color(jb->valueint), true);
            tap_target(l, on_tap_air, e); ka++;
        }
        fmt_notable(e, buf, sizeof buf);
        if (buf[0]) tap_target(line(cn, buf, &lv_font_montserrat_14, vc, false), on_tap_notable, e);
    }
    if (!kt) line(ct, "-", &lv_font_montserrat_20, C_DIM, true);
    if (!kh) line(ch, "-", &lv_font_montserrat_20, C_DIM, true);
    if (!kd) line(cd, "-", &lv_font_montserrat_20, C_DIM, true);
    if (!ka) line(ca, "-", &lv_font_montserrat_20, C_DIM, true);
}

// s_shown <- the rooms in `rows`: saved order first (those still present), then the rest in server order
static bool in_list(char (*ids)[ID_LEN], int n, const char *id)
{
    for (int i = 0; i < n; i++) if (strcmp(ids[i], id) == 0) return true;
    return false;
}

static const char *row_room(cJSON *e)
{
    cJSON *j = cJSON_GetObjectItem(e, "room");
    return cJSON_IsString(j) ? j->valuestring : "";
}

static void build_order(cJSON *rows)
{
    char seen[MAX_ROOMS][ID_LEN];
    int nseen = 0;
    cJSON *e;
    cJSON_ArrayForEach(e, rows) {
        const char *rm = row_room(e);
        if (nseen < MAX_ROOMS && !in_list(seen, nseen, rm)) snprintf(seen[nseen++], ID_LEN, "%s", rm);
    }
    s_nshown = 0;
    for (int i = 0; i < s_norder; i++)
        if (in_list(seen, nseen, s_order[i]) && !in_list(s_shown, s_nshown, s_order[i]))
            memcpy(s_shown[s_nshown++], s_order[i], ID_LEN);
    for (int i = 0; i < nseen; i++)
        if (!in_list(s_shown, s_nshown, seen[i])) memcpy(s_shown[s_nshown++], seen[i], ID_LEN);
}

static const char *room_name_of(cJSON *rows, const char *room)
{
    cJSON *e;
    cJSON_ArrayForEach(e, rows) {
        if (strcmp(row_room(e), room) != 0) continue;
        cJSON *j = cJSON_GetObjectItem(e, "room_name");
        return cJSON_IsString(j) ? j->valuestring : room;
    }
    return room;
}

// ── render ───────────────────────────────────────────────────────────────────
void ui_sensors_render(cJSON *doc, lv_obj_t *parent, void (*changed)(void),
                       void (*open_graph)(const char *device_id, const char *metric))
{
    s_changed = changed;
    s_open_graph = open_graph;
    lv_coord_t y = lv_obj_get_scroll_y(parent);
    lv_obj_clean(parent);                                       // deletes the labels pointing into s_rows...

    cJSON *src = doc ? cJSON_GetObjectItem(doc, "sensors") : NULL;
    if (s_rows) cJSON_Delete(s_rows);                           // ...so the old copy can go
    s_rows = cJSON_IsArray(src) ? cJSON_Duplicate(src, 1) : NULL;
    cJSON *rows = s_rows;
    if (!cJSON_IsArray(rows)) {
        cell(row_box(parent, false), "No sensor data (offline?)", 0, &lv_font_montserrat_20, C_DIM, false);
        return;
    }

    // count the hidden ones that still exist (a hidden id whose sensor is gone doesn't count), and the rooms
    // that still have a visible sensor
    build_order(rows);
    int total = 0, nhid = 0, nroom = 0;
    char vis[MAX_ROOMS][ID_LEN];
    cJSON *e;
    cJSON_ArrayForEach(e, rows) {
        cJSON *jid = cJSON_GetObjectItem(e, "device_id");
        if (!cJSON_IsString(jid)) continue;
        total++;
        if (find_hidden(jid->valuestring) >= 0) { nhid++; continue; }
        if (nroom < MAX_ROOMS && !in_list(vis, nroom, row_room(e))) snprintf(vis[nroom++], ID_LEN, "%s", row_room(e));
    }

    // toolbar: grid -> [ n rooms - m sensors (k hidden) ] ... [ Edit ];  edit -> [ tap to hide ] ... [ Done ]
    lv_obj_t *bar = row_box(parent, false);
    char t[80];
    if (s_edit) snprintf(t, sizeof t, LV_SYMBOL_UP LV_SYMBOL_DOWN " move a room   " LV_SYMBOL_EYE_CLOSE " hide a sensor");
    else if (nhid) snprintf(t, sizeof t, "%d rooms  -  %d sensors (%d hidden)", nroom, total - nhid, nhid);
    else snprintf(t, sizeof t, "%d rooms  -  %d sensors", nroom, total);
    cell(bar, t, 0, &lv_font_montserrat_20, C_DIM, false);
    if (!s_edit && s_sort_col != SORT_NONE) button(bar, LV_SYMBOL_REFRESH "  Your order", 200, on_sort, (void *)(intptr_t)SORT_NONE);
    button(bar, s_edit ? LV_SYMBOL_OK "  Done" : LV_SYMBOL_EDIT "  Edit", 160, on_edit, NULL);

    // column header (in the grid, Room/Temp/Hum/Dew/Air are tap-to-sort)
    lv_obj_t *hdr = row_box(parent, false);
    if (s_edit) {
        cell(hdr, "Sensor", W_NAME, &lv_font_montserrat_14, C_HEAD, false);
        cell(hdr, "Temp", W_TEMP, &lv_font_montserrat_14, C_HEAD, true);
        cell(hdr, "Hum", W_HUM, &lv_font_montserrat_14, C_HEAD, true);
        cell(hdr, "Dew", W_DEW, &lv_font_montserrat_14, C_HEAD, true);
        cell(hdr, "Air", W_AIR, &lv_font_montserrat_14, C_HEAD, true);
    } else {
        lv_obj_set_style_pad_ver(hdr, 6, 0);
        sort_head(hdr, "Room", W_ROOM, SORT_ROOM, false);
        sort_head(hdr, "Temp", W_TEMP, SORT_TEMP, true);
        sort_head(hdr, "Hum", W_HUM, SORT_HUM, true);
        sort_head(hdr, "Dew", W_DEW, SORT_DEW, true);
        sort_head(hdr, "Air", W_AIR, SORT_AIR, true);
    }
    cell(hdr, "Other", 0, &lv_font_montserrat_14, C_HEAD, false);
    if (s_edit) { lv_obj_t *pad = lv_label_create(hdr); lv_label_set_text(pad, ""); lv_obj_set_width(pad, W_BTN); }

    if (!s_edit) {
        int nent = 0;
        for (int k = 0; k < s_nshown; k++) {                    // rooms in this panel's order
            room_ent_t *r = &s_ent[nent];
            r->n = 0;
            cJSON_ArrayForEach(e, rows) {
                cJSON *jid = cJSON_GetObjectItem(e, "device_id");
                if (!cJSON_IsString(jid) || find_hidden(jid->valuestring) >= 0) continue;
                if (strcmp(row_room(e), s_shown[k]) == 0 && r->n < MAX_PER_ROOM) r->ss[r->n++] = e;
            }
            if (!r->n) continue;                                // every sensor in it hidden
            r->slug = s_shown[k];
            r->name = room_name_of(rows, s_shown[k]);
            sort_key(r);
            nent++;
        }
        int idx[MAX_ROOMS];
        for (int i = 0; i < nent; i++) idx[i] = i;
        if (s_sort_col != SORT_NONE)                            // insertion sort: stable, ties keep your order
            for (int i = 1; i < nent; i++)
                for (int j = i; j > 0 && sort_cmp(&s_ent[idx[j - 1]], &s_ent[idx[j]]) > 0; j--) {
                    int tmp = idx[j]; idx[j] = idx[j - 1]; idx[j - 1] = tmp;
                }
        for (int i = 0; i < nent; i++) room_row(parent, s_ent[idx[i]].name, s_ent[idx[i]].ss, s_ent[idx[i]].n);
        lv_obj_update_layout(parent);
        lv_obj_scroll_to_y(parent, y, LV_ANIM_OFF);             // keep the place across the 10 s refresh
        return;
    }

    int nrow = 0;
    for (int k = 0; k < s_nshown; k++) {
        // room heading: name ... [up] [down] (no arrow past either end)
        char rn[40];
        ascii_fold(room_name_of(rows, s_shown[k]), rn, sizeof rn);
        lv_obj_t *rh = row_box(parent, false);
        lv_obj_set_style_pad_top(rh, 6, 0);
        cell(rh, rn, 0, &lv_font_montserrat_20, C_HEAD, false);
        if (k > 0) button(rh, LV_SYMBOL_UP, W_BTN, on_room_up, (void *)(intptr_t)k);
        else { lv_obj_t *sp = lv_label_create(rh); lv_label_set_text(sp, ""); lv_obj_set_width(sp, W_BTN); }
        if (k < s_nshown - 1) button(rh, LV_SYMBOL_DOWN, W_BTN, on_room_down, (void *)(intptr_t)k);
        else { lv_obj_t *sp = lv_label_create(rh); lv_label_set_text(sp, ""); lv_obj_set_width(sp, W_BTN); }

        cJSON_ArrayForEach(e, rows) {
            cJSON *jid = cJSON_GetObjectItem(e, "device_id");
            if (!cJSON_IsString(jid) || nrow >= MAX_ROWS || strcmp(row_room(e), s_shown[k]) != 0) continue;
            bool hidden = find_hidden(jid->valuestring) >= 0;

            uint32_t vc = is_stale(e) ? C_DIM : C_TEXT;
            lv_obj_t *r = row_box(parent, true);
            if (hidden) lv_obj_set_style_opa(r, LV_OPA_50, 0);

            char buf[160];
            cJSON *jnm = cJSON_GetObjectItem(e, "name");
            ascii_fold(cJSON_IsString(jnm) ? jnm->valuestring : jid->valuestring, buf, sizeof buf);
            cell(r, buf, W_NAME, &lv_font_montserrat_20, vc, false);
            fmt_temp(cJSON_GetObjectItem(e, "temperature_c"), buf, sizeof buf);
            cell(r, buf, W_TEMP, &lv_font_montserrat_20, vc, true);
            cJSON *jh = cJSON_GetObjectItem(e, "humidity_pct");
            if (cJSON_IsNumber(jh)) snprintf(buf, sizeof buf, "%d%%", jh->valueint); else snprintf(buf, sizeof buf, "-");
            cell(r, buf, W_HUM, &lv_font_montserrat_20, vc, true);
            fmt_temp(cJSON_GetObjectItem(e, "dewpoint_c"), buf, sizeof buf);
            cell(r, buf, W_DEW, &lv_font_montserrat_20, vc, true);
            cJSON *jb = cJSON_GetObjectItem(e, "air_band");
            cJSON *jbl = cJSON_GetObjectItem(e, "air_band_label");
            if (cJSON_IsNumber(jb) && cJSON_IsString(jbl)) {
                ascii_fold(jbl->valuestring, buf, sizeof buf);
                cell(r, buf, W_AIR, &lv_font_montserrat_20, vc == C_DIM ? C_DIM : aq_band_color(jb->valueint), true);
            } else {
                cell(r, "-", W_AIR, &lv_font_montserrat_20, C_DIM, true);
            }
            fmt_notable(e, buf, sizeof buf);
            cell(r, buf, 0, &lv_font_montserrat_14, vc, false);

            snprintf(s_row_id[nrow], ID_LEN, "%s", jid->valuestring);
            button(r, hidden ? LV_SYMBOL_EYE_OPEN : LV_SYMBOL_EYE_CLOSE, W_BTN, on_hide_toggle, s_row_id[nrow]);
            nrow++;
        }
    }

    lv_obj_update_layout(parent);
    lv_obj_scroll_to_y(parent, y, LV_ANIM_OFF);                 // keep the place across the 10 s refresh
}
