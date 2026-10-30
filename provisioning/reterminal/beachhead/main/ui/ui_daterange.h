// Custom date range picker (Catherine, 2026-10-30) for the D1001 Graphs view — LVGL has no date input,
// so this is a modal on lv_layer_top(): Start = [day roller][hour roller], End = the same, then Apply /
// Cancel (a tap on the backdrop cancels too). Days run from Today back UI_DATERANGE_DAYS-1 days
// ("Today", "Yesterday", "Tue 10/28"); hours are 00:00..23:00 local. An End at the current hour or later
// means "until now"; End must be after Start (the card says so and stays open).
#pragma once
#include <time.h>

#define UI_DATERANGE_DAYS 90

// Open the picker preset to [t0, t1] (epoch seconds; rounded to the hour). `cb(t0, t1)` is called on a
// valid Apply. Needs the wall clock set (the caller checks). Caller holds the LVGL lock.
void ui_daterange_open(time_t t0, time_t t1, void (*cb)(time_t t0, time_t t1));
