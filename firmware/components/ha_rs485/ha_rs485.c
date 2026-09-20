// ha_rs485 — half-duplex RS-485 UART transport (ADR-0041).
// Thin IDF glue; the arithmetic lives in ha_rs485_timing.c, which is pure and host-tested.
// See include/ha_rs485.h for the contract.
#include "ha_rs485.h"

#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_rom_sys.h"

static const char *TAG = "ha_rs485";

#define DEFAULT_RX_BUF 2048u

static uint8_t stop_bits_x2(uart_stop_bits_t sb) {
    switch (sb) {
    case UART_STOP_BITS_1_5: return 3u;   // 1.5
    case UART_STOP_BITS_2:   return 4u;   // 2.0
    default:                 return 2u;   // 1.0
    }
}

static uint8_t data_bit_count(uart_word_length_t wl) {
    switch (wl) {
    case UART_DATA_5_BITS: return 5u;
    case UART_DATA_6_BITS: return 6u;
    case UART_DATA_7_BITS: return 7u;
    default:               return 8u;
    }
}

esp_err_t ha_rs485_init(ha_rs485_t *b, const ha_rs485_cfg_t *cfg) {
    if (!b || !cfg) return ESP_ERR_INVALID_ARG;
    if (cfg->baud == 0) return ESP_ERR_INVALID_ARG;
    if (cfg->tx_gpio < 0 || cfg->rx_gpio < 0) return ESP_ERR_INVALID_ARG;

    memset(b, 0, sizeof(*b));
    b->cfg = *cfg;
    if (b->cfg.rx_buf_bytes == 0) b->cfg.rx_buf_bytes = DEFAULT_RX_BUF;
    // The IDF driver requires an RX ring larger than the hardware FIFO (128).
    if (b->cfg.rx_buf_bytes < 256) b->cfg.rx_buf_bytes = 256;

    b->auto_direction = (cfg->de_gpio < 0);
    b->bits_per_char  = ha_rs485_bits_per_char(data_bit_count(cfg->data_bits),
                                               cfg->parity != UART_PARITY_DISABLE,
                                               stop_bits_x2(cfg->stop_bits));
    b->turnaround_us  = ha_rs485_turnaround_us(cfg->baud, b->bits_per_char, b->auto_direction);

    uart_config_t uc = {
        .baud_rate  = (int)cfg->baud,
        .data_bits  = cfg->data_bits,
        .parity     = cfg->parity,
        .stop_bits  = cfg->stop_bits,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    esp_err_t err = uart_param_config(cfg->port, &uc);
    if (err != ESP_OK) { ESP_LOGE(TAG, "uart_param_config: %s", esp_err_to_name(err)); return err; }

    // RTS carries DE when the transceiver has one; otherwise leave the pin alone.
    int rts = b->auto_direction ? UART_PIN_NO_CHANGE : cfg->de_gpio;
    err = uart_set_pin(cfg->port, cfg->tx_gpio, cfg->rx_gpio, rts, UART_PIN_NO_CHANGE);
    if (err != ESP_OK) { ESP_LOGE(TAG, "uart_set_pin: %s", esp_err_to_name(err)); return err; }

    // TX buffer of 0 makes uart_write_bytes block until the bytes are handed to the FIFO, which is what
    // we want on a half-duplex bus: the write call and the wire stay in step.
    err = uart_driver_install(cfg->port, (int)b->cfg.rx_buf_bytes, 0, 0, NULL, 0);
    if (err != ESP_OK) { ESP_LOGE(TAG, "uart_driver_install: %s", esp_err_to_name(err)); return err; }

    if (!b->auto_direction) {
        // The UART drives DE in hardware with exact turnaround — strictly better than an RC one-shot.
        err = uart_set_mode(cfg->port, UART_MODE_RS485_HALF_DUPLEX);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "uart_set_mode(RS485_HALF_DUPLEX): %s", esp_err_to_name(err));
            uart_driver_delete(cfg->port);
            return err;
        }
    }

    b->inited = true;
    ESP_LOGI(TAG, "uart%d %" PRIu32 " baud, %s direction, rx_buf=%u%s",
             (int)cfg->port, cfg->baud,
             b->auto_direction ? "auto" : "hardware DE",
             (unsigned)b->cfg.rx_buf_bytes,
             cfg->listen_only ? ", LISTEN-ONLY (writes refused)" : "");
    if (cfg->listen_only) {
        ESP_LOGW(TAG, "listen-only: this port will NOT transmit. Clear cfg.listen_only to take the bus.");
    }
    return ESP_OK;
}

esp_err_t ha_rs485_deinit(ha_rs485_t *b) {
    if (!b || !b->inited) return ESP_ERR_INVALID_STATE;
    esp_err_t err = uart_driver_delete(b->cfg.port);
    b->inited = false;
    return err;
}

// Read back and drop only the bytes that genuinely match what we just sent. Anything that diverges is a
// real reply and is held for the next read — the IDF driver has no push-back, and eating a reply would be
// a silent data loss that looks like a protocol bug much later.
static void suppress_echo(ha_rs485_t *b, const uint8_t *sent, size_t len) {
    uint8_t tmp[HA_RS485_MAX_TX];
    uint32_t window_ms = (b->turnaround_us / 1000u) + 2u;

    int n = uart_read_bytes(b->cfg.port, tmp, len, pdMS_TO_TICKS(window_ms));
    if (n <= 0) return;

    size_t match = 0;
    while (match < (size_t)n && match < len && tmp[match] == sent[match]) match++;

    b->stats.echo_bytes += match;

    size_t extra = (size_t)n - match;
    if (extra > 0) {
        if (extra > sizeof(b->hold)) extra = sizeof(b->hold);   // cannot happen: n <= len <= MAX_TX
        memcpy(b->hold, &tmp[match], extra);
        b->hold_len = extra;
        b->hold_pos = 0;
        if (match == 0) {
            ESP_LOGD(TAG, "discard_echo: %u byte(s) were not echo, held", (unsigned)extra);
        }
    }
}

esp_err_t ha_rs485_write(ha_rs485_t *b, const uint8_t *data, size_t len, uint32_t timeout_ms) {
    if (!b || !b->inited || (!data && len)) return ESP_ERR_INVALID_ARG;
    if (len > HA_RS485_MAX_TX) return ESP_ERR_INVALID_SIZE;

    if (b->cfg.listen_only) {
        b->stats.writes_refused++;
        ESP_LOGW(TAG, "write of %u byte(s) refused: listen-only", (unsigned)len);
        return ESP_ERR_NOT_SUPPORTED;
    }
    if (len == 0) return ESP_OK;

    int written = uart_write_bytes(b->cfg.port, (const char *)data, len);
    if (written < 0 || (size_t)written != len) {
        ESP_LOGE(TAG, "uart_write_bytes wrote %d of %u", written, (unsigned)len);
        return ESP_FAIL;
    }
    b->stats.tx_bytes += len;

    // Wait for the line to actually clear. Without this the caller cannot know when it is safe to listen,
    // and on a half-duplex bus that matters. Default the timeout generously from the real TX duration.
    if (timeout_ms == 0) {
        timeout_ms = (ha_rs485_tx_us(b->cfg.baud, b->bits_per_char, (uint32_t)len) / 1000u) + 20u;
    }
    esp_err_t err = uart_wait_tx_done(b->cfg.port, pdMS_TO_TICKS(timeout_ms));
    if (err != ESP_OK) {
        b->stats.tx_timeouts++;
        ESP_LOGW(TAG, "uart_wait_tx_done: %s", esp_err_to_name(err));
        return err;
    }

    // Auto-direction transceivers hold the driver enabled briefly past the last stop bit; listening
    // through that window reads the tail of our own transmission.
    if (b->turnaround_us) esp_rom_delay_us(b->turnaround_us);

    if (b->cfg.discard_echo) suppress_echo(b, data, len);
    return ESP_OK;
}

int ha_rs485_read(ha_rs485_t *b, uint8_t *out, size_t max, uint32_t timeout_ms) {
    if (!b || !b->inited || !out || max == 0) return -1;

    size_t n = 0;

    while (b->hold_pos < b->hold_len && n < max) out[n++] = b->hold[b->hold_pos++];
    if (b->hold_pos >= b->hold_len) { b->hold_len = 0; b->hold_pos = 0; }
    if (n == max) { b->stats.rx_bytes += n; return (int)n; }

    // Having already produced bytes, top up without blocking — returning promptly beats waiting for a
    // full buffer the peer may never send.
    TickType_t ticks = (n > 0) ? 0 : pdMS_TO_TICKS(timeout_ms);
    int r = uart_read_bytes(b->cfg.port, out + n, max - n, ticks);
    if (r < 0) return (n > 0) ? (int)n : -1;

    n += (size_t)r;
    b->stats.rx_bytes += n;
    return (int)n;
}

esp_err_t ha_rs485_flush_input(ha_rs485_t *b) {
    if (!b || !b->inited) return ESP_ERR_INVALID_STATE;
    b->hold_len = 0;
    b->hold_pos = 0;
    return uart_flush_input(b->cfg.port);
}

void ha_rs485_get_stats(const ha_rs485_t *b, ha_rs485_stats_t *out) {
    if (!b || !out) return;
    *out = b->stats;
}

bool ha_rs485_is_listen_only(const ha_rs485_t *b) {
    return b && b->inited && b->cfg.listen_only;
}

uint32_t ha_rs485_tx_time_us(const ha_rs485_t *b, uint32_t nbytes) {
    if (!b || !b->inited) return 0;
    return ha_rs485_tx_us(b->cfg.baud, b->bits_per_char, nbytes);
}
