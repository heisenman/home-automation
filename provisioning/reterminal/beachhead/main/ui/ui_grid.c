// Sensor tile grid (see ui_grid.h). Extracted from ui_tiles.c verbatim (ADR-0020 module-first);
// behavior identical. card_for -> ui_grid_add_card(e, parent); apply_state -> ui_grid_apply_state.
// Tap-to-expand now hands ui_expand an explicit seed (below) instead of a card index, so ui_expand
// needs no knowledge of this module's registry.
#include "ui/ui_grid.h"
#include "ui/ui_format.h"   // catalog + metric_of/disp_val/ascii_fold/parse_hex_color + gspec/MAX_GRAPHS
#include "ui/ui_expand.h"   // expand_open + struct expand_seed
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include "lvgl.h"
#include "esp_lvgl_port.h"
#include "cJSON.h"
#include "esp_log.h"

static const char *TAG = "ui.grid";

// Per-card registry so MQTT state updates can patch a card's headline in place.
// Written by ui_grid_add_card() and read by ui_grid_apply_state() — both hold the LVGL lock,
// which serializes them (render rebuilds the whole registry each fetch).
struct card_ref {
    char id[40];
    char hkey[24], hlabel[20], hunit[12];   // headline metric spec (copied from the server catalog)
    int hprec;
    lv_obj_t *hval;                       // the headline label widget
    char name[40];                        // display name (for the detail overlay)
    char room[28];
    char *detail;                         // heap: all metrics, one per line (freed on re-render)
    struct gspec graphs[MAX_GRAPHS];      // graphable metrics for this sensor (server spec)
    int ngraph;
};
#define MAX_CARDS 48
static struct card_ref s_cards[MAX_CARDS];
static int s_ncards;

void ui_grid_reset(void)
{
    for (int i = 0; i < s_ncards; i++) { free(s_cards[i].detail); s_cards[i].detail = NULL; }
    s_ncards = 0;                     // registry rebuilt from scratch each fetch
}

static void card_clicked_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx < 0 || idx >= s_ncards) return;
    struct card_ref *c = &s_cards[idx];
    ESP_LOGI(TAG, "tap -> card %d (%s)", idx, c->id);
    struct expand_seed seed = {
        .id = c->id, .name = c->name, .room = c->room,
        .detail = c->detail, .g = c->graphs, .ngraph = c->ngraph,
    };
    expand_open(&seed);
}

void ui_grid_add_card(cJSON *e, lv_obj_t *parent)
{
    cJSON *metrics = cJSON_GetObjectItem(e, "metrics");
    if (!cJSON_IsObject(metrics)) return;

    const cJSON *jname = cJSON_GetObjectItem(e, "name");
    const cJSON *jroom = cJSON_GetObjectItem(e, "room");
    const cJSON *jage  = cJSON_GetObjectItem(e, "age_s");
    const cJSON *jid   = cJSON_GetObjectItem(e, "device_id");
    const char *name = cJSON_IsString(jname) ? jname->valuestring
                     : (cJSON_IsString(jid) ? jid->valuestring : "sensor");
    const char *room = cJSON_IsString(jroom) ? jroom->valuestring : "";
    int age = cJSON_IsNumber(jage) ? (int)jage->valuedouble : -1;
    bool stale = (age < 0 || age > 600);   // >10 min = stale

    // Unified air-quality band (ADR-0035, gas nodes): render as a colored badge instead of a plain number.
    cJSON *aqr = cJSON_GetObjectItem(e, "air_quality_report");
    int aq_band = 0; const char *aq_label = NULL; bool aq_rel = false;
    if (cJSON_IsObject(aqr)) {
        const cJSON *jb  = cJSON_GetObjectItem(aqr, "air_quality_band");
        const cJSON *jl  = cJSON_GetObjectItem(aqr, "air_quality_band_label");
        const cJSON *jbz = cJSON_GetObjectItem(aqr, "air_quality_basis");
        if (cJSON_IsNumber(jb) && cJSON_IsString(jl)) {
            aq_band  = (int)jb->valuedouble;
            aq_label = jl->valuestring;
            aq_rel   = cJSON_IsString(jbz) && strcmp(jbz->valuestring, "relative") == 0;
        }
    }

    lv_obj_t *card = lv_obj_create(parent);
    lv_obj_set_size(card, 236, 158);
    lv_obj_set_style_bg_color(card, lv_color_hex(stale ? 0x161a24 : 0x16204a), 0);
    lv_obj_set_style_border_width(card, 0, 0);
    lv_obj_set_style_radius(card, 12, 0);
    lv_obj_set_style_pad_all(card, 10, 0);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *t = lv_label_create(card);
    lv_label_set_text(t, name);
    lv_obj_set_style_text_font(t, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(t, lv_color_hex(stale ? 0x64748b : 0xffffff), 0);
    lv_label_set_long_mode(t, LV_LABEL_LONG_DOT);
    lv_obj_set_width(t, lv_pct(100));

    lv_obj_t *r = lv_label_create(card);
    if (age >= 0) lv_label_set_text_fmt(r, "%s  -  %ds", room, age);
    else          lv_label_set_text(r, room);
    lv_obj_set_style_text_color(r, lv_color_hex(0x8fb4ff), 0);

    // headline = highest-priority present metric (catalog order), shown big
    int ncat; const struct mfmt *cat = ui_format_catalog(&ncat);
    char big[48] = ""; int headline = -1;
    for (int i = 0; i < ncat && headline < 0; i++) {
        bool p; double v = metric_of(metrics, cat[i].key, &p);
        if (p) { headline = i; snprintf(big, sizeof(big), "%.*f %s", cat[i].prec,
                                        disp_val(cat[i].unit, v), disp_unit(cat[i].unit)); }
    }
    if (headline >= 0) {
        lv_obj_t *h = lv_label_create(card);
        lv_label_set_text_fmt(h, "%s %s", cat[headline].label, big);
        lv_obj_set_style_text_font(h, &lv_font_montserrat_28, 0);
        lv_obj_set_style_text_color(h, lv_color_hex(stale ? 0x94a3b8 : 0xffffff), 0);
        lv_obj_set_style_pad_top(h, 4, 0);
        // register for live MQTT patching + tap-to-detail
        if (s_ncards < MAX_CARDS && cJSON_IsString(jid)) {
            int idx = s_ncards;
            struct card_ref *cr = &s_cards[s_ncards++];
            snprintf(cr->id, sizeof(cr->id), "%s", jid->valuestring);
            snprintf(cr->hkey, sizeof(cr->hkey), "%s", cat[headline].key);
            snprintf(cr->hlabel, sizeof(cr->hlabel), "%s", cat[headline].label);
            snprintf(cr->hunit, sizeof(cr->hunit), "%s", cat[headline].unit);
            cr->hprec = cat[headline].prec;
            cr->hval = h;
            snprintf(cr->name, sizeof(cr->name), "%s", name);
            snprintf(cr->room, sizeof(cr->room), "%s", room);
            char det[400]; size_t o = 0;                 // full detail: every catalog metric, one per line
            for (int i = 0; i < ncat; i++) {
                bool pp; double vv = metric_of(metrics, cat[i].key, &pp);
                if (!pp) continue;
                o += snprintf(det + o, sizeof(det) - o, "%s%s: %.*f %s",
                              o ? "\n" : "", cat[i].label, cat[i].prec,
                              disp_val(cat[i].unit, vv), disp_unit(cat[i].unit));
                if (o >= sizeof(det) - 24) break;
            }
            cr->detail = strdup(det);
            // server-authored graph spec (ADR-0019): which metrics to chart + label/color/precision
            cr->ngraph = 0;
            cJSON *graphs = cJSON_GetObjectItem(e, "graphs");
            if (cJSON_IsArray(graphs)) {
                cJSON *g;
                cJSON_ArrayForEach(g, graphs) {
                    if (cr->ngraph >= MAX_GRAPHS) break;
                    const cJSON *gk = cJSON_GetObjectItem(g, "key");
                    const cJSON *gl = cJSON_GetObjectItem(g, "label");
                    const cJSON *gu = cJSON_GetObjectItem(g, "unit");
                    const cJSON *gc = cJSON_GetObjectItem(g, "color");
                    const cJSON *gp = cJSON_GetObjectItem(g, "precision");
                    if (!cJSON_IsString(gk)) continue;
                    struct gspec *gs = &cr->graphs[cr->ngraph++];
                    snprintf(gs->key, sizeof(gs->key), "%s", gk->valuestring);
                    ascii_fold(cJSON_IsString(gl) ? gl->valuestring : gk->valuestring,
                               gs->label, sizeof(gs->label));
                    ascii_fold(cJSON_IsString(gu) ? gu->valuestring : "", gs->unit, sizeof(gs->unit));
                    gs->color = parse_hex_color(cJSON_IsString(gc) ? gc->valuestring : NULL);
                    gs->prec = cJSON_IsNumber(gp) ? (int)gp->valuedouble : 0;
                }
            }
            lv_obj_add_flag(card, LV_OBJ_FLAG_CLICKABLE);
            lv_obj_add_event_cb(card, card_clicked_cb, LV_EVENT_CLICKED, (void *)(intptr_t)idx);
        }
    }

    // unified air-quality band badge (ADR-0035) — colored, between the headline and the metric row
    if (aq_band) {
        char folded[24]; ascii_fold(aq_label, folded, sizeof(folded));
        lv_obj_t *aqb = lv_label_create(card);
        lv_label_set_text_fmt(aqb, "%s%s AQ", folded, aq_rel ? " ~" : "");
        lv_obj_set_style_text_font(aqb, &lv_font_montserrat_20, 0);
        lv_obj_set_style_text_color(aqb, lv_color_hex(aq_band_color(aq_band)), 0);
        lv_obj_set_style_pad_top(aqb, 4, 0);
    }

    // remaining present metrics as a compact multiline
    char rest[192] = ""; size_t off = 0;
    for (int i = 0; i < ncat; i++) {
        if (i == headline) continue;
        if (aq_band && strcmp(cat[i].key, "air_quality") == 0) continue;  // shown as the colored band badge above
        bool p; double v = metric_of(metrics, cat[i].key, &p);
        if (!p) continue;
        off += snprintf(rest + off, sizeof(rest) - off, "%s%s %.*f%s",
                        off ? "   " : "", cat[i].label, cat[i].prec,
                        disp_val(cat[i].unit, v), disp_unit(cat[i].unit));
        if (off >= sizeof(rest) - 24) break;
    }
    if (rest[0]) {
        lv_obj_t *s = lv_label_create(card);
        lv_label_set_text(s, rest);
        lv_label_set_long_mode(s, LV_LABEL_LONG_WRAP);
        lv_obj_set_width(s, lv_pct(100));
        lv_obj_set_style_text_color(s, lv_color_hex(0x94a3b8), 0);
        lv_obj_set_style_pad_top(s, 4, 0);
    }
}

// Parse one state payload and patch the matching card's headline (holds the LVGL
// lock). Runs on state_task — NEVER on the MQTT callback stack (LVGL is too heavy
// for it: the retained state burst would overflow the mqtt task and reboot).
void ui_grid_apply_state(const char *json)
{
    cJSON *root = cJSON_Parse(json);
    if (!root) return;
    const cJSON *jid = cJSON_GetObjectItem(root, "device_id");
    cJSON *metrics = cJSON_GetObjectItem(root, "metrics");
    if (cJSON_IsString(jid) && cJSON_IsObject(metrics) && lvgl_port_lock(0)) {
        for (int i = 0; i < s_ncards; i++) {
            if (strcmp(s_cards[i].id, jid->valuestring) != 0) continue;
            bool p; double v = metric_of(metrics, s_cards[i].hkey, &p);
            if (p && s_cards[i].hval) {
                // NB: format with newlib snprintf, then lv_label_set_text — LVGL's
                // built-in printf does NOT support %f/%.*f and would crash (desync).
                char buf[48];
                snprintf(buf, sizeof(buf), "%s %.*f %s",
                         s_cards[i].hlabel, s_cards[i].hprec,
                         disp_val(s_cards[i].hunit, v), disp_unit(s_cards[i].hunit));
                lv_label_set_text(s_cards[i].hval, buf);
            }
            break;
        }
        lvgl_port_unlock();
    }
    cJSON_Delete(root);
}
