// BREADCRUMB: firmware/components > ha_audio - shared audible-alert output (ES8311 codec + I2S TX + PA gate).
// Contract: ADR-0020. Parent: firmware/AGENTS.md. Roadmap #6.
// REUSE-WHEN: a board with an ES8311 codec → Class-D amp → speaker needs to emit local alert tones.
//
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "driver/i2c_master.h"

// Shared audible-output component (ADR-0020). Brings up an I2S-std TX channel + the ES8311 codec (playback
// path only) and emits synthesized alert tones through the board's Class-D amp. The amp's enable line is a
// platform seam (a callback) — on the D1001 it's PCA9535 expander pin 11, not a P4 GPIO, so the caller wires
// it to its existing io-expander. The ES8311 sits on the caller's ALREADY-initialized new-driver I2C bus
// (D1001: bsp_i2c1()), shared with the RTC/IMU/expander — this component adds itself as a device, it does
// NOT install an I2C driver. Playback runs on a private worker task; callers never block. Docs-first map:
// docs/hardware/reterminal-d1001.md (Audio subsystem).
typedef struct {
    i2c_master_bus_handle_t i2c_bus;   // bus the ES8311 is on (D1001: bsp_i2c1() = I2C_1, SCL21/SDA20)
    uint16_t codec_addr;               // ES8311 7-bit address (D1001: 0x18, CE low)
    int mclk_io, bclk_io, ws_io, dout_io;  // DAC I2S pins (D1001: 33, 32, 31, 30)
    int sample_rate;                   // Hz; 16000 matches the vendor coeff table + is plenty for alerts
    int volume;                        // 0..100 initial ES8311 output volume
    // Enable/disable the power amp (NS4150B). Called with the audio worker holding a play; the component
    // gates the amp only while sound is playing (avoids idle amp hiss). NULL => amp assumed always-on.
    void (*pa_enable)(bool on, void *user);
    void *user;
} ha_audio_cfg_t;

// Bring up I2S TX + ES8311 (playback). Idempotent-safe to call once at boot. Returns ESP_OK on success;
// on failure the beep/chime calls become no-ops (a dead codec must never wedge the caller).
esp_err_t ha_audio_init(const ha_audio_cfg_t *cfg);

// True once init succeeded.
bool ha_audio_ready(void);

// Set ES8311 output volume 0..100 (applied to subsequent playback).
void ha_audio_set_volume(int pct);

// Queue a single tone (sine). freq_hz e.g. 880; ms duration; amp_pct 0..100 loudness. Non-blocking:
// returns immediately, the worker plays it (amp gated on for the duration).
void ha_audio_beep(int freq_hz, int ms, int amp_pct);

// Queue a short two-note alert chime (device-local alert sound). Non-blocking.
void ha_audio_chime(void);
