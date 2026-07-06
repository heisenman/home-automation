// Metric presentation + catalog helpers (see ui_format.h). Extracted from ui_tiles.c
// verbatim as the first ADR-0020 module-first split; behavior identical.
#include "ui/ui_format.h"
#include <stdio.h>
#include <stdlib.h>

// Metric presentation spec = the server's shared UI catalog (/api/v1/sensors top-level `metrics`,
// ADR-0019). SINGLE source of label/unit/precision/order truth, mirroring the PWA — no hardcoded
// table. Order = display + headline priority. A tiny fallback covers the pre-first-fetch window (or
// an older server that doesn't emit the catalog) until ui_format_load_catalog() replaces it.
#define MAX_CAT 32
static struct mfmt s_cat[MAX_CAT] = {
    {"temperature_c", "Temp", "C", 1, true},
    {"humidity_pct",  "Hum",  "%", 0, true},
};
static int s_ncat = 2;   // fallback entry count until the server catalog arrives

void ascii_fold(const char *src, char *dst, size_t cap)
{
    size_t o = 0;
    for (const unsigned char *p = (const unsigned char *)src; *p && o < cap - 1; ) {
        if (*p < 0x80) { dst[o++] = *p++; continue; }
        char sub = 0; int adv = 0;
        if (p[0] == 0xC2 && p[1] == 0xB0) { sub = 0;   adv = 2; }      // ° -> drop
        else if (p[0] == 0xC2 && p[1] == 0xB5) { sub = 'u'; adv = 2; } // µ
        else if (p[0] == 0xC2 && p[1] == 0xB2) { sub = '2'; adv = 2; } // ²
        else if (p[0] == 0xC2 && p[1] == 0xB3) { sub = '3'; adv = 2; } // ³
        else if (p[0] == 0xE2 && p[1] == 0x82 && p[2] == 0x82) { sub = '2'; adv = 3; } // ₂
        else if (p[0] == 0xE2 && p[1] == 0x82 && p[2] == 0x83) { sub = '3'; adv = 3; } // ₃
        else { p++; while ((*p & 0xC0) == 0x80) p++; continue; }       // unknown: skip whole char
        if (sub && o < cap - 1) dst[o++] = sub;
        p += adv;
    }
    dst[o] = 0;
}

void ui_format_load_catalog(cJSON *metrics)
{
    if (!cJSON_IsArray(metrics) || cJSON_GetArraySize(metrics) == 0) return;   // keep the fallback
    int n = 0; cJSON *m;
    cJSON_ArrayForEach(m, metrics) {
        if (n >= MAX_CAT) break;
        const cJSON *k = cJSON_GetObjectItem(m, "key");
        if (!cJSON_IsString(k)) continue;
        const cJSON *l = cJSON_GetObjectItem(m, "label");
        const cJSON *u = cJSON_GetObjectItem(m, "unit");
        const cJSON *pr = cJSON_GetObjectItem(m, "precision");
        struct mfmt *f = &s_cat[n++];
        snprintf(f->key, sizeof(f->key), "%s", k->valuestring);
        ascii_fold(cJSON_IsString(l) ? l->valuestring : k->valuestring, f->label, sizeof(f->label));
        ascii_fold(cJSON_IsString(u) ? u->valuestring : "", f->unit, sizeof(f->unit));
        f->prec = cJSON_IsNumber(pr) ? (int)pr->valuedouble : 0;
        f->core = cJSON_IsTrue(cJSON_GetObjectItem(m, "core"));
    }
    if (n > 0) s_ncat = n;
}

const struct mfmt *ui_format_catalog(int *count)
{
    if (count) *count = s_ncat;
    return s_cat;
}

double metric_of(cJSON *metrics, const char *key, bool *present)
{
    cJSON *v = cJSON_GetObjectItem(metrics, key);
    *present = cJSON_IsNumber(v);
    return *present ? v->valuedouble : 0.0;
}

uint32_t parse_hex_color(const char *s)
{
    if (s && s[0] == '#' && strlen(s) >= 7) {
        uint32_t v = (uint32_t)strtoul(s + 1, NULL, 16);
        return v & 0xFFFFFF;
    }
    return 0x8fb4ff;
}
