// Server-backed LVGL tile renderer (ADR-0019 Phase 2).
//   Fetches the BFF sensor view-model (GET /api/v1/sensors) over HTTP and paints
//   a scrolling grid of sensor cards on the panel, refreshing on an interval.
//   Read-only for now; touch->signed-commands + the UI manifest come next.
#pragma once

// Take over the screen (clears the splash) and start the fetch+render loop.
// `sensors_url` = full URL of the BFF sensors endpoint. Safe to call once, after
// the display is up. Non-fatal: fetch/parse failures just retry next interval.
void ui_tiles_start(const char *sensors_url);

// Live update from a retained MQTT `home/<area>/<id>/state` payload (JSON string):
// merges the device's headline metric into its card between HTTP refreshes.
// No-op if the UI isn't started or the device has no card. Safe from any task.
void ui_tiles_on_state(const char *json_payload);
