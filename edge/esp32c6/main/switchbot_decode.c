// SwitchBot BLE advertisement decoder — C port of server/ingest/decoders/switchbot.py.
// Keep behaviour identical to the Python so edge and server-scanned readings agree.
#include "switchbot_decode.h"
#include <string.h>
#include <stdio.h>

// Model byte (svc[0] & 0x7F) -> device_type label. Mirrors _MODEL_NAMES.
static const char *model_name(uint8_t model_byte) {
    switch (model_byte & 0x7F) {
        case 0x54: return "switchbot_meter";          // 'T' WoSensorTH
        case 0x69: return "switchbot_meter_plus";     // 'i' WoSensorTHPlus
        case 0x34: return "switchbot_meter_pro";      // '4' WoSensorTHPro (Gen2)
        case 0x4F: return "switchbot_meter_pro";      // 'O' WoSensorTHPro
        case 0x50: return "switchbot_meter_pro";      // 'P' WoSensorTHPro alt
        case 0x77: return "switchbot_meter_outdoor";  // 'w' WoSensorTHO
        default:   return NULL;
    }
}

bool sb_is_switchbot(bool has_0969_mfr, bool has_fd3d_svc) {
    return has_0969_mfr || has_fd3d_svc;
}

// temp = int(b_int & 0x7F) + (b_frac & 0x0F)*0.1 ; sign = b_int & 0x80 (set => positive)
static bool parse_th(uint8_t b_frac, uint8_t b_int, uint8_t b_hum,
                     float *temp_out, int *hum_out) {
    float temp = (float)(b_int & 0x7F) + (float)(b_frac & 0x0F) * 0.1f;
    if (!(b_int & 0x80)) temp = -temp;
    int hum = b_hum & 0x7F;
    if (temp < -40.0f || temp > 60.0f) return false;   // validation ranges
    if (hum < 0 || hum > 100) return false;
    *temp_out = temp;
    *hum_out = hum;
    return true;
}

bool sb_decode(const uint8_t *svc, int svc_len,
               const uint8_t *mfr, int mfr_len,
               sb_reading_t *out) {
    memset(out, 0, sizeof(*out));
    out->battery_pct = -1;
    out->valid = false;

    // device_type from service-data byte 0 (model byte), for all known variants
    const char *dt = NULL;
    if (svc && svc_len >= 1) dt = model_name(svc[0]);
    snprintf(out->device_type, sizeof(out->device_type), "%s", dt ? dt : "switchbot_meter");

    // Battery: service-data byte 2 for ALL meters (the documented fd3d layout).
    if (svc && svc_len >= 3) out->battery_pct = svc[2] & 0x7F;

    float temp; int hum;
    if (svc && svc_len >= 6) {
        // Format A: full service data (Meter / Plus / older Pro): svc[3,4,5]
        if (!parse_th(svc[3], svc[4], svc[5], &temp, &hum)) return false;
    } else if (mfr && mfr_len >= 11) {
        // Format B: manufacturer data with 6-byte MAC prefix (Outdoor / newer Pro):
        // bytes after MAC are mfr[6..]; temp at mfr[8,9], humidity mfr[10].
        if (!parse_th(mfr[8], mfr[9], mfr[10], &temp, &hum)) return false;
    } else {
        return false;
    }

    // round temperature to 1 decimal (match Python round(.,1))
    out->temperature_c = (float)((int)(temp * 10.0f + (temp >= 0 ? 0.5f : -0.5f))) / 10.0f;
    out->humidity_pct = hum;
    out->valid = true;
    return true;
}
