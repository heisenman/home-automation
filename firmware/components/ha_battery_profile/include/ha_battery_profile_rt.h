// Runtime side of the versioned battery profile (ADR-0024 §5): PERSISTENCE (NVS) + TRANSPORT (JSON).
// Deliberately knows NOTHING about ha_battery — it only produces/consumes validated ha_batt_profile_t
// structs (the pure core's header promise: "the runtime side hands back one of these structs"). The
// caller wires an accepted profile into the gauge via ha_battery_set_profile(). Keeping load/parse/
// validate policy in one reusable place means every battery device deploys curves the same way: a
// better profile is pushed as DATA (MQTT/NVS), no reflash — versioned, comparable, rollback-able.
#pragma once
#include "ha_battery_profile.h"
#include "esp_err.h"

// Load the persisted profile from NVS into *out. ESP_OK if a valid stored profile was loaded;
// ESP_ERR_NVS_NOT_FOUND if none is stored (out untouched — caller falls back to the baked default);
// ESP_ERR_INVALID_STATE if a stored blob failed validation (ignored, treat as none). Other = NVS error.
esp_err_t ha_batt_profile_rt_load(ha_batt_profile_t *out);

// Persist p to NVS (validated first; ESP_ERR_INVALID_ARG if invalid). Survives reboot.
esp_err_t ha_batt_profile_rt_save(const ha_batt_profile_t *p);

// Erase the persisted profile so the next boot falls back to the baked-in default.
esp_err_t ha_batt_profile_rt_clear(void);

// Parse a pushed JSON profile into *out and validate it (ha_batt_profile_valid). ESP_OK on success;
// ESP_FAIL on bad JSON / schema / validation, with a short reason written to err (if non-NULL). Schema
// mirrors ha_batt_profile_rt_to_json: {version,date,method, off_*_mv, run_floor_mv, warn_mv,
// warn_clear_mv, boot_gate_mv, boot_release_mv, lut:[...]}.
esp_err_t ha_batt_profile_rt_from_json(const char *json, ha_batt_profile_t *out, char *err, int err_cap);

// Emit p as a compact JSON object (+ an optional `source` tag: "nvs"|"default"|"pushed") into buf.
// Returns the length written (snprintf semantics), or -1 on error. For the retained /profile status.
int ha_batt_profile_rt_to_json(const ha_batt_profile_t *p, const char *source, char *buf, int cap);
