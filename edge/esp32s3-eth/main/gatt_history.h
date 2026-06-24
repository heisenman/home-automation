#pragma once
#include <stdbool.h>
// Pull a SwitchBot meter's on-device history over GATT and relay the raw notifications to
// home/edge/<node>/<mac>/history (decoded server-side by edge_history.py). profile: "meter_pro"
// or "outdoor". Cancels the passive scan for the duration; resumes it on disconnect.
// Only one pull at a time; returns false if a pull is already in progress or args are bad.
bool gatt_history_pull(const char *mac_str, const char *profile);
// True while a pull is active (scanner stays paused).
bool gatt_history_busy(void);
