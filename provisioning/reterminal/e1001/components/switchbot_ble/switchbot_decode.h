// SwitchBot BLE advertisement decoder (C port of server/ingest/decoders/switchbot.py).
// Pure, no ESP/Bluetooth deps — unit-testable on the host.
#pragma once
#include <stdint.h>
#include <stdbool.h>

#define SB_MFR_COMPANY_ID 0x0969   // manufacturer-specific company id
#define SB_SVC_UUID16     0xFD3D   // 16-bit service-data UUID

typedef struct {
    char  device_type[28];   // e.g. "switchbot_meter_outdoor"
    float temperature_c;
    int   humidity_pct;
    int   battery_pct;       // -1 if not present
    bool  valid;             // true if temperature+humidity decoded and in range
} sb_reading_t;

// svc/mfr point at the payload AFTER the UUID / company-id (matching the Python dicts):
//   svc = service-data bytes after the 0xFD3D UUID  (svc_len = length, 0 if absent)
//   mfr = manufacturer bytes after the 0x0969 id    (mfr_len = length, 0 if absent)
bool sb_is_switchbot(bool has_0969_mfr, bool has_fd3d_svc);

// Returns true and fills *out (out->valid set) when this is a decodable SwitchBot meter.
bool sb_decode(const uint8_t *svc, int svc_len,
               const uint8_t *mfr, int mfr_len,
               sb_reading_t *out);
