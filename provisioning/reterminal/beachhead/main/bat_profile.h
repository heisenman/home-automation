// Battery ADC discharge profiler (D1001).
//
// Mounts the microSD card (SDMMC 4-bit, Slot 0, on-chip LDO ch4) and appends a CSV row of
// full battery telemetry every ~15 s to /sdcard/battprofile.csv. Purpose: capture the raw
// ADC readback across a full 100%->0% discharge so the voltage->SoC LUT can be re-fit
// empirically, and to validate the BAT_READ_EN + smoothing fix over the whole range.
//
// Non-fatal: if the SD card is absent/unmountable it logs and returns an error; the panel's
// normal (server-backed) operation is unaffected. `publish` (optional) mirrors each Nth row
// to MQTT for live watching without pulling the card; pass NULL to log to SD only.
#pragma once
#include "esp_err.h"

esp_err_t bat_profile_start(void (*publish)(const char *json));
