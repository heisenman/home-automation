// Device-local per-scene backlight policy (ADR-0019 Phase 2; D1001 roadmap #2 device-side pivot).
//
// A wall panel is a command-and-control INTERFACE: its own backlight is a device-LOCAL setting, so
// the scene->brightness policy lives HERE, in the device, NOT as a networked actuator on the server
// control plane (Hugh's durable rule — see feedback-cnc-local-settings / ADR-0019). The panel already
// learns the active house scene from GET /api/v1/house (ui_scenes); on a scene CHANGE the composition
// root looks the scene up in this table and drives the backlight locally.
//
// The table is data, not code: it boots from a baked-in default (Home=100, Away=40, Sleep=0), can be
// overridden live via MQTT (cmd/scene-brightness, mirroring the battery-profile push in ADR-0024 §5),
// and persists to NVS so the override survives reboot with no reflash. Pure policy: no LVGL, no I2C,
// no actuation — the caller maps the returned percent onto bsp_display_* (0 => sleep, N>0 => wake+level).
#pragma once
#include "esp_err.h"
#include <stddef.h>

// Load the persisted table from NVS, or seed the baked-in default if none is stored. Call once at boot.
void scene_dim_init(void);

// Look up the backlight percent for a scene name (case-sensitive, matches the /api/v1/house scene
// string). Returns 0..100 on a hit, or -1 if the scene is not in the table (caller should leave the
// current brightness untouched rather than guess).
int scene_dim_lookup(const char *scene);

// Replace the table from a JSON object of {"<scene>": <pct 0..100>, ...} and persist it to NVS
// (hot, no reflash). Returns ESP_OK, or ESP_ERR_INVALID_ARG with *err filled on a malformed payload.
esp_err_t scene_dim_set_from_json(const char *json, char *err, size_t errlen);

// Drop any persisted override and revert to the baked-in default table. Persists the clear.
void scene_dim_reset_default(void);

// Serialize the active table as a JSON object {"source":"default|nvs|pushed","table":{...}} into buf.
// Returns the byte count written (excluding NUL), or 0 on error. For the retained status topic.
int scene_dim_to_json(char *buf, size_t buflen);
