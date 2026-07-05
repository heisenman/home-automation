// Audible-alert output: I2S-std TX + ES8311 codec (playback) + PA gate. The codec bring-up mirrors the
// Seeed BSP reference (driver_examples/01_I2SCodec) verbatim-in-behaviour — same es8311_init / clock /
// sample-freq / volume sequence — differing only where the panel requires it: the ES8311 lives on the
// caller's NEW-driver I2C bus (bsp_i2c1) via the adapted vendored es8311/ driver, and the amp enable is a
// callback (D1001: PCA9535 pin 11). Tones are synthesized on a private worker so callers never block.
#include "ha_audio.h"
#include "es8311.h"
#include <string.h>
#include <math.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "driver/i2s_std.h"

static const char *TAG = "ha_audio";

#define MCLK_MULTIPLE   384          // matches the vendor coeff table (256 min for <=16bit; 384 is safe)
#define CHUNK_FRAMES    256          // stereo frames per I2S write

typedef struct { int freq; int ms; int amp; } tone_req_t;

static bool                 s_ready;
static bool                 s_enabled = true;   // master gate ("very easy to disable"); caller persists it
static ha_audio_cfg_t       s_cfg;
static i2s_chan_handle_t    s_tx;
static es8311_handle_t      s_codec;
static QueueHandle_t        s_q;
static int16_t              s_buf[CHUNK_FRAMES * 2];   // interleaved L/R

bool ha_audio_ready(void) { return s_ready; }

void ha_audio_set_volume(int pct) {
    if (!s_ready) return;
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    es8311_voice_volume_set(s_codec, pct, NULL);
}

// Synthesize one tone straight to I2S. Runs on the worker task (amp already enabled). A short linear
// fade in/out (~4ms) suppresses the on/off click.
static void play_tone(int freq, int ms, int amp_pct) {
    if (freq < 20) freq = 20;
    if (amp_pct < 0) amp_pct = 0;
    if (amp_pct > 100) amp_pct = 100;
    const int rate = s_cfg.sample_rate;
    long total = (long)rate * ms / 1000;
    if (total <= 0) return;
    const int fade = rate * 4 / 1000;                 // ~4ms ramp
    const double step = 2.0 * M_PI * freq / rate;
    const double peak = 0.6 * 32767.0 * amp_pct / 100.0;   // headroom below full scale
    double phase = 0;
    long done = 0;
    while (done < total) {
        int n = CHUNK_FRAMES;
        if (n > total - done) n = (int)(total - done);
        for (int i = 0; i < n; i++) {
            long k = done + i;
            double env = 1.0;
            if (k < fade)            env = (double)k / fade;
            else if (k > total-fade) env = (double)(total - k) / fade;
            int16_t s = (int16_t)(peak * env * sin(phase));
            phase += step; if (phase > 2*M_PI) phase -= 2*M_PI;
            s_buf[i*2] = s; s_buf[i*2 + 1] = s;        // L = R
        }
        size_t wrote = 0;
        i2s_channel_write(s_tx, s_buf, n * 2 * sizeof(int16_t), &wrote, pdMS_TO_TICKS(1000));
        done += n;
    }
}

static void audio_worker(void *arg) {
    (void)arg;
    tone_req_t req;
    bool amp_on = false;
    for (;;) {
        // Block for the first request; once playing, keep the amp on across a short gap so a multi-note
        // chime doesn't click between notes. Drain, then power the amp down when idle.
        if (xQueueReceive(s_q, &req, amp_on ? pdMS_TO_TICKS(180) : portMAX_DELAY) == pdTRUE) {
            if (!amp_on && s_cfg.pa_enable) { s_cfg.pa_enable(true, s_cfg.user); vTaskDelay(pdMS_TO_TICKS(8)); }
            amp_on = true;
            play_tone(req.freq, req.ms, req.amp);
        } else if (amp_on) {
            if (s_cfg.pa_enable) s_cfg.pa_enable(false, s_cfg.user);
            amp_on = false;
        }
    }
}

void ha_audio_set_enabled(bool on) { s_enabled = on; }
bool ha_audio_enabled(void) { return s_enabled; }

void ha_audio_beep(int freq_hz, int ms, int amp_pct) {
    if (!s_ready || !s_q || !s_enabled) return;   // master gate: disabled => silent no-op
    tone_req_t r = { .freq = freq_hz, .ms = ms, .amp = amp_pct };
    xQueueSend(s_q, &r, 0);
}

void ha_audio_chime(void) {
    // Pleasant two-note rising alert (A5 -> E6).
    ha_audio_beep(880, 140, 70);
    ha_audio_beep(1319, 180, 70);
}

static esp_err_t codec_init(void) {
    s_codec = es8311_create(s_cfg.i2c_bus, s_cfg.codec_addr);
    if (!s_codec) { ESP_LOGE(TAG, "es8311_create failed"); return ESP_FAIL; }
    const es8311_clock_config_t clk = {
        .mclk_inverted = false,
        .sclk_inverted = false,
        .mclk_from_mclk_pin = true,
        .mclk_frequency = s_cfg.sample_rate * MCLK_MULTIPLE,
        .sample_frequency = s_cfg.sample_rate,
    };
    esp_err_t e = es8311_init(s_codec, &clk, ES8311_RESOLUTION_16, ES8311_RESOLUTION_16);
    if (e != ESP_OK) return e;
    e = es8311_sample_frequency_config(s_codec, s_cfg.sample_rate * MCLK_MULTIPLE, s_cfg.sample_rate);
    if (e != ESP_OK) return e;
    es8311_voice_volume_set(s_codec, s_cfg.volume, NULL);
    es8311_microphone_config(s_codec, false);   // playback-only; mic path unused for alerts
    return ESP_OK;
}

static esp_err_t i2s_init(void) {
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.auto_clear = true;                 // zero the DMA tail so silence stays silent
    esp_err_t e = i2s_new_channel(&chan_cfg, &s_tx, NULL);   // TX only (no capture)
    if (e != ESP_OK) return e;
    i2s_std_config_t std = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(s_cfg.sample_rate),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = s_cfg.mclk_io, .bclk = s_cfg.bclk_io, .ws = s_cfg.ws_io,
            .dout = s_cfg.dout_io, .din = I2S_GPIO_UNUSED,
            .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
        },
    };
    std.clk_cfg.mclk_multiple = MCLK_MULTIPLE;
    e = i2s_channel_init_std_mode(s_tx, &std);
    if (e != ESP_OK) return e;
    return i2s_channel_enable(s_tx);
}

esp_err_t ha_audio_init(const ha_audio_cfg_t *cfg) {
    if (!cfg || !cfg->i2c_bus) return ESP_ERR_INVALID_ARG;
    if (s_ready) return ESP_OK;
    s_cfg = *cfg;
    if (s_cfg.sample_rate <= 0) s_cfg.sample_rate = 16000;
    if (s_cfg.codec_addr == 0)  s_cfg.codec_addr = 0x18;

    esp_err_t e = i2s_init();
    if (e != ESP_OK) { ESP_LOGE(TAG, "i2s init failed: %s", esp_err_to_name(e)); return e; }
    e = codec_init();
    if (e != ESP_OK) { ESP_LOGE(TAG, "codec init failed: %s", esp_err_to_name(e)); return e; }

    s_q = xQueueCreate(8, sizeof(tone_req_t));
    if (!s_q) return ESP_ERR_NO_MEM;
    xTaskCreate(audio_worker, "audio", 3072, NULL, 4, NULL);
    s_ready = true;
    ESP_LOGI(TAG, "ready: ES8311@0x%02x on I2S(mclk=%d bclk=%d ws=%d dout=%d) rate=%d",
             s_cfg.codec_addr, s_cfg.mclk_io, s_cfg.bclk_io, s_cfg.ws_io, s_cfg.dout_io, s_cfg.sample_rate);
    return ESP_OK;
}
