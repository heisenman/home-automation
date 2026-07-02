// SD file-ops over MQTT (D1001).
//
// A small remote filesystem surface so the microSD can be driven live — list, stat, read,
// write, delete, mkdir, df — without reflashing. Commands arrive as JSON on cmd/fs; each is
// handled off the mqtt-callback stack (worker task + queue, the v11/v17 blocking-IO lesson) and
// a JSON result is published back. Paths are scoped to the SD mount (/sdcard) for safety.
//
// Command JSON (one op per message):
//   {"op":"ls","path":"/sdcard[/dir]"}                        -> {entries:[{name,size,dir}...]}
//   {"op":"stat","path":"/sdcard/f"}                          -> {exists,size,dir}
//   {"op":"read","path":"/sdcard/f","off":0,"len":1024}       -> {n,size,eof,b64}
//   {"op":"write","path":"/sdcard/f","text|b64":"...","append":false} -> {n}
//   {"op":"rm","path":"/sdcard/f"}                            -> {ok}
//   {"op":"mkdir","path":"/sdcard/d"}                         -> {ok}
//   {"op":"df"}                                               -> {total,free}
// An optional "id" in the command is echoed back for correlation. Errors: {"ok":false,"err":..}.
#pragma once
#include "esp_err.h"

// Start the worker. `publish` mirrors each JSON response to MQTT (e.g. d1001-beachhead/fs).
esp_err_t fs_ops_start(void (*publish)(const char *json));

// Enqueue a raw JSON command string (copied). Safe to call from the mqtt-callback stack.
void fs_ops_submit(const char *json_cmd);
