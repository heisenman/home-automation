#pragma once
#include <stdbool.h>

// Generic GATT step-interpreter. The server composes a list of BLE steps (subscribe / write /
// write-sequence / read / collect / delay); this connects to `mac`, discovers all characteristics,
// runs the steps in order, and streams replies on home/edge/<node>/<reqid>/reply. This is the
// generalization of gatt_history.c — any device interaction is expressed as data, no new firmware.
//
// `steps_json` is the raw JSON array text of the "steps" field (copied; caller may free after).
// Returns false if the node is busy with another central-role op, the mac isn't in the scan cache,
// or the connect can't be started.
bool gatt_exec_run(const char *reqid, const char *mac_str, const char *steps_json);

bool gatt_exec_busy(void);
