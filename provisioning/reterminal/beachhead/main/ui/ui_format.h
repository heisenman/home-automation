// Metric presentation + catalog helpers — extracted from ui_tiles.c (ADR-0020 module-first).
//
// This module owns the shared UI *metric spec*: the label/unit/precision/order catalog
// (mirroring the server /api/v1/sensors `metrics`), the graph spec type, the ascii-fold
// for LVGL's ASCII-only font, and the panel-local Fahrenheit localization. It has NO
// dependencies on other ui/* modules (a leaf), so it extracts first.
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include "cJSON.h"

// One metric's presentation spec (label/unit/precision/core), copied from the server catalog.
// `core` (server metric_spec.core) = kept in the map's 0b tier when secondary metrics are dropped.
struct mfmt { char key[24], label[20], unit[12]; int prec; bool core; };

// Server-authored graph spec for a metric (from /api/v1/sensors `graphs`, ADR-0019 shared UI spec).
// Shared type: the grid card registry and the expand panels both carry an array of these.
#define MAX_GRAPHS 8
struct gspec { char key[16]; char label[18]; char unit[8]; uint32_t color; int prec; };

// LVGL's montserrat font is ASCII-only; the server labels/units carry °, µ, ³, ₂ etc. Fold the
// known multibyte glyphs to ASCII (°→"", µ→u, ²/³→2/3, ₂/₃→2/3) and drop any other non-ASCII.
void ascii_fold(const char *src, char *dst, size_t cap);

// "#rrggbb" -> 0xrrggbb (server graph color). Falls back to a neutral slate on any parse miss.
uint32_t parse_hex_color(const char *s);

// Unified air-quality band (5 Excellent .. 1 Very Poor, ADR-0035) -> 0xrrggbb, mirroring the PWA's
// AQ_BAND_COLOR (EPA green→yellow→orange→red→purple). Unknown/absent band -> neutral slate.
uint32_t aq_band_color(int band);

// Read a numeric metric from a cJSON metrics object; *present set to whether it was a number.
double metric_of(cJSON *metrics, const char *key, bool *present);

// Rebuild the metric catalog from the server `metrics` array (keeps the fallback if empty/absent).
// Caller holds the LVGL lock (invoked from render()).
void ui_format_load_catalog(cJSON *metrics);

// Borrow the current metric catalog (display + headline-priority order). *count set to entry count.
const struct mfmt *ui_format_catalog(int *count);

// Panel-local unit localization: this wall panel displays Fahrenheit. Any Celsius metric
// (server unit "C") is converted for DISPLAY only; the data and the shared spec stay SI.
static inline bool is_celsius(const char *unit) { return unit && strcmp(unit, "C") == 0; }
static inline double disp_val(const char *unit, double v) { return is_celsius(unit) ? v * 9.0 / 5.0 + 32.0 : v; }
static inline const char *disp_unit(const char *unit) { return is_celsius(unit) ? "F" : unit; }
