// aprilaire_sniff — see aprilaire_sniff.h. Listen-only; protocol-agnostic framing by idle gap.
#include "aprilaire_sniff.h"

#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "ha_rs485.h"

static const char *TAG = "apr_sniff";

// Pins: docs/edge-pinouts.md + docs/design/hvac-shade-device-integration.md §"Node builds" (same as hvac_c6).
// NEVER D6/D7 (GPIO16/17 = UART0 console) and never UART_NUM_0.
#define SNIFF_UART      UART_NUM_1
#define SNIFF_TX_GPIO   18          // D10 — configured (ha_rs485 requires it) but never written; wire may be absent
#define SNIFF_RX_GPIO   20          // D9  — the transceiver's data-out
#define SNIFF_BAUD      9600        // E070 Remote bus per prior art (9600 8N1)
#define GAP_MS          10          // >~9 char times of silence at 9600 baud ends a frame
#define REPORT_MS       30000
#define FRAME_MAX       64          // bytes kept per frame (longer frames are counted, truncated in the log)
#define RING            6           // most recent frames kept for the report

typedef struct { uint8_t b[FRAME_MAX]; uint16_t len; uint16_t full_len; uint32_t t_ms; } frame_t;

static ha_rs485_t   s_bus;
static void       (*s_log)(const char *fmt, ...);
static SemaphoreHandle_t s_mu;
static frame_t      s_ring[RING];
static unsigned     s_ring_n;                  // total frames ever seen (ring index = n % RING)
static uint32_t     s_first_ms, s_last_ms;
static unsigned     s_lead[256];               // histogram of each frame's first byte

static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }

static void frame_done(const uint8_t *buf, size_t len, size_t full_len) {
    xSemaphoreTake(s_mu, portMAX_DELAY);
    frame_t *f = &s_ring[s_ring_n % RING];
    f->len = (uint16_t)len;
    f->full_len = (uint16_t)full_len;
    f->t_ms = now_ms();
    memcpy(f->b, buf, len);
    if (!s_ring_n) s_first_ms = f->t_ms;
    s_last_ms = f->t_ms;
    s_lead[buf[0]]++;
    s_ring_n++;
    xSemaphoreGive(s_mu);
}

static void fmt_frame(const frame_t *f, char *out, size_t cap) {
    size_t o = 0;
    for (size_t i = 0; i < f->len && o + 4 < cap; i++) o += snprintf(out + o, cap - o, "%02X", f->b[i]);
    o += snprintf(out + o, cap - o, " |");
    for (size_t i = 0; i < f->len && o + 3 < cap; i++)
        out[o++] = (f->b[i] >= 0x20 && f->b[i] < 0x7F) ? (char)f->b[i] : '.';
    snprintf(out + o, cap - o, "|%s", f->full_len > f->len ? " (truncated)" : "");
}

void aprilaire_sniff_report(void) {
    if (!s_log || !s_bus.inited) return;
    ha_rs485_stats_t t;
    ha_rs485_get_stats(&s_bus, &t);
    xSemaphoreTake(s_mu, portMAX_DELAY);
    unsigned n = s_ring_n;
    uint32_t span = s_last_ms - s_first_ms;
    // top 3 lead bytes — an ASCII 'M'/'R' here would match the prior-art frame types
    int top[3] = {-1, -1, -1};
    for (int c = 0; c < 256; c++) {
        if (!s_lead[c]) continue;
        for (int k = 0; k < 3; k++)
            if (top[k] < 0 || s_lead[c] > s_lead[top[k]]) {
                for (int m = 2; m > k; m--) top[m] = top[m - 1];
                top[k] = c;
                break;
            }
    }
    xSemaphoreGive(s_mu);

    const char *verdict =
        t.rx_bytes == 0 ? "SILENT — either the E070 sends nothing with no remote attached (the question), or "
                          "the wiring: data-out on D9, A/B on the E070 A/B, PE on Remote '-'. Try the other TTL pad."
        : n < 2         ? "BYTES BUT NO FRAME STRUCTURE yet — if this persists, suspect A/B swapped or wrong baud "
                          "(swapping A/B is non-destructive)."
                        : "HEARING FRAMES — see the raw dump below.";
    s_log("apr-sniff: rx=%llu B frames=%u over %lus | writes_refused=%lu | lead bytes: %02X x%u, %02X x%u, %02X x%u | %s",
          (unsigned long long)t.rx_bytes, n, (unsigned long)(span / 1000), (unsigned long)t.writes_refused,
          top[0] < 0 ? 0 : top[0], top[0] < 0 ? 0 : s_lead[top[0]],
          top[1] < 0 ? 0 : top[1], top[1] < 0 ? 0 : s_lead[top[1]],
          top[2] < 0 ? 0 : top[2], top[2] < 0 ? 0 : s_lead[top[2]], verdict);

    char line[FRAME_MAX * 3 + 32];
    unsigned shown = n < RING ? n : RING;
    for (unsigned i = 0; i < shown; i++) {
        xSemaphoreTake(s_mu, portMAX_DELAY);
        frame_t f = s_ring[(n - shown + i) % RING];
        xSemaphoreGive(s_mu);
        fmt_frame(&f, line, sizeof line);
        s_log("apr-sniff: frame[-%u] len=%u: %s", shown - i, f.full_len, line);
    }
}

static void sniff_task(void *arg) {
    (void)arg;
    uint8_t chunk[64], frame[FRAME_MAX];
    size_t flen = 0, full = 0;
    uint32_t last_report = now_ms();
    for (;;) {
        int n = ha_rs485_read(&s_bus, chunk, sizeof chunk, GAP_MS);
        if (n > 0) {
            for (int i = 0; i < n; i++) {
                if (flen < FRAME_MAX) frame[flen++] = chunk[i];
                full++;
            }
        } else if (full) {                       // a GAP_MS silence closes the frame
            frame_done(frame, flen, full);
            flen = full = 0;
        }
        if ((uint32_t)(now_ms() - last_report) >= REPORT_MS) {
            aprilaire_sniff_report();
            last_report = now_ms();
        }
    }
}

bool aprilaire_sniff_start(void (*log)(const char *fmt, ...)) {
    s_log = log;
    s_mu = xSemaphoreCreateMutex();
    esp_err_t err = ha_rs485_init(&s_bus, &(ha_rs485_cfg_t){
        .port = SNIFF_UART, .tx_gpio = SNIFF_TX_GPIO, .rx_gpio = SNIFF_RX_GPIO, .de_gpio = -1,
        .baud = SNIFF_BAUD, .data_bits = UART_DATA_8_BITS, .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1, .listen_only = true });
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ha_rs485_init failed: %s", esp_err_to_name(err));
        return false;
    }
    xTaskCreate(sniff_task, "apr_sniff", 4096, NULL, 4, NULL);
    ESP_LOGW(TAG, "Aprilaire A/B sniffer up: UART1 RX=GPIO%d @%d 8N1, LISTEN-ONLY", SNIFF_RX_GPIO, SNIFF_BAUD);
    return true;
}
