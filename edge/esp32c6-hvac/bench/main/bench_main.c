// hvac bench self-test — exercises the RS-485 lane and the relay on the hvac/dehum node hardware before it
// goes anywhere near the Broan or the Aprilaire. No WiFi, no MQTT, no NVS: a line console on USB-Serial-JTAG,
// driven by tools/hvac_bench.py. Pins are the node's, from docs/edge-pinouts.md — keep them in step.
//
// ⛔ Bench only. This image TRANSMITS on RS-485 with no listen-only gate. Never flash it onto a node that is
// wired to the ERV bus.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/uart.h"
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "ha_rs485.h"

#define RS485_PORT     UART_NUM_1     // same as the node — never UART_NUM_0
#define RS485_TX_GPIO  18             // silk D10 -> transceiver RXD
#define RS485_RX_GPIO  20             // silk D9  <- transceiver TXD
#define BAUD           38400u         // Broan

// The only pins `relay` may drive. Anything else is refused so a typo cannot toggle a strapping pin.
static const int kRelayPins[] = { 19 /* D8 Broan OVR */, 1 /* D1 Aprilaire DH */ };

static ha_rs485_t s_bus;
static volatile bool s_tx_parked;     // TX pin taken off the UART and held as a GPIO

static int64_t now_ms(void) { return esp_timer_get_time() / 1000; }

static bool relay_pin_ok(int pin) {
    for (size_t i = 0; i < sizeof kRelayPins / sizeof kRelayPins[0]; i++)
        if (kRelayPins[i] == pin) return true;
    return false;
}

static void relays_safe(void) {
    for (size_t i = 0; i < sizeof kRelayPins / sizeof kRelayPins[0]; i++) {
        int p = kRelayPins[i];
        gpio_reset_pin(p);
        gpio_set_level(p, 0);
        gpio_set_direction(p, GPIO_MODE_INPUT_OUTPUT);   // INPUT too, so `relay` can read back the pin
        gpio_set_pull_mode(p, GPIO_PULLDOWN_ONLY);
        gpio_set_level(p, 0);
    }
}

// Reports every byte the transceiver hands us, plus the RX pin's own level. The level watch is independent
// of the UART: a held low (a battery across A/B, or a second driver) shows up even if no valid byte forms.
static void rx_task(void *arg) {
    uint8_t buf[64];
    int last_level = gpio_get_level(RS485_RX_GPIO);
    int64_t low_since = 0;
    for (;;) {
        // 20 ms, not less: below one tick the read returns at once and this task starves the console.
        int n = ha_rs485_read(&s_bus, buf, sizeof buf, 20);
        if (n > 0) {
            printf("RX %d:", n);
            for (int i = 0; i < n; i++) printf(" %02X", buf[i]);
            printf("\n");
        }
        int lvl = gpio_get_level(RS485_RX_GPIO);
        if (lvl != last_level) {
            int64_t t = now_ms();
            if (lvl == 0) low_since = t;
            else if (t - low_since >= 20) printf("RXPIN low for %lld ms\n", (long long)(t - low_since));
            last_level = lvl;
        }
    }
}

static void tx_burst(uint8_t byte, int count) {
    uint8_t chunk[HA_RS485_MAX_TX];
    memset(chunk, byte, sizeof chunk);
    int left = count;
    while (left > 0) {
        int n = left > (int)sizeof chunk ? (int)sizeof chunk : left;
        esp_err_t err = ha_rs485_write(&s_bus, chunk, n, 1000);
        if (err != ESP_OK) { printf("ERR tx %s\n", esp_err_to_name(err)); return; }
        left -= n;
    }
}

static void tx_park(int level) {
    gpio_reset_pin(RS485_TX_GPIO);
    gpio_set_direction(RS485_TX_GPIO, GPIO_MODE_OUTPUT);
    gpio_set_level(RS485_TX_GPIO, level);
    s_tx_parked = true;
}

static void tx_unpark(void) {
    uart_set_pin(RS485_PORT, RS485_TX_GPIO, RS485_RX_GPIO, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    s_tx_parked = false;
}

static void handle(char *line) {
    char *argv[4] = {0};
    int argc = 0;
    for (char *t = strtok(line, " \t"); t && argc < 4; t = strtok(NULL, " \t")) argv[argc++] = t;
    if (argc == 0) return;
    const char *cmd = argv[0];

    if (!strcmp(cmd, "relay") && argc == 3) {
        int pin = atoi(argv[1]);
        if (!relay_pin_ok(pin)) { printf("ERR relay pin %d not allowed\n", pin); return; }
        int on = !strcmp(argv[2], "on");
        gpio_set_level(pin, on);
        printf("OK relay gpio%d=%d readback=%d\n", pin, on, gpio_get_level(pin));
    } else if (!strcmp(cmd, "tx") && argc == 3) {
        if (s_tx_parked) tx_unpark();
        uint8_t b = (uint8_t)strtol(argv[1], NULL, 16);
        int count = atoi(argv[2]);
        ha_rs485_stats_t before; ha_rs485_get_stats(&s_bus, &before);
        tx_burst(b, count);
        ha_rs485_stats_t after; ha_rs485_get_stats(&s_bus, &after);
        printf("OK tx %02X x%d sent=%llu\n", b, count, (unsigned long long)(after.tx_bytes - before.tx_bytes));
    } else if (!strcmp(cmd, "txpin") && argc == 2) {
        if (!strcmp(argv[1], "uart")) { tx_unpark(); printf("OK txpin uart\n"); }
        else { int lvl = atoi(argv[1]) ? 1 : 0; tx_park(lvl); printf("OK txpin gpio%d=%d\n", RS485_TX_GPIO, lvl); }
    } else if (!strcmp(cmd, "rxlevel")) {
        printf("OK rxlevel=%d\n", gpio_get_level(RS485_RX_GPIO));
    } else if (!strcmp(cmd, "stats")) {
        ha_rs485_stats_t s; ha_rs485_get_stats(&s_bus, &s);
        printf("OK stats rx=%llu tx=%llu echo=%llu refused=%lu timeouts=%lu\n",
               (unsigned long long)s.rx_bytes, (unsigned long long)s.tx_bytes,
               (unsigned long long)s.echo_bytes, (unsigned long)s.writes_refused, (unsigned long)s.tx_timeouts);
    } else if (!strcmp(cmd, "safe")) {
        relays_safe();
        if (s_tx_parked) tx_unpark();
        printf("OK safe\n");
    } else {
        printf("ERR unknown: relay <19|1> on|off | tx <hexbyte> <n> | txpin 0|1|uart | rxlevel | stats | safe\n");
    }
}

void app_main(void) {
    relays_safe();   // first thing: the relay IN floats from reset until this runs

    usb_serial_jtag_driver_config_t uj = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    usb_serial_jtag_driver_install(&uj);
    usb_serial_jtag_vfs_use_driver();   // printf and our reads share the driver, not the raw FIFO

    ESP_ERROR_CHECK(ha_rs485_init(&s_bus, &(ha_rs485_cfg_t){
        .port = RS485_PORT, .tx_gpio = RS485_TX_GPIO, .rx_gpio = RS485_RX_GPIO, .de_gpio = -1,
        .baud = BAUD, .data_bits = UART_DATA_8_BITS, .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1, .listen_only = false, .discard_echo = false,
    }));
    xTaskCreate(rx_task, "rx", 3072, NULL, 5, NULL);
    printf("BENCH READY uart=%d tx=%d rx=%d baud=%u relays=19,1\n", RS485_PORT, RS485_TX_GPIO, RS485_RX_GPIO,
           (unsigned)BAUD);

    char line[96];
    size_t len = 0;
    for (;;) {
        uint8_t c;
        if (usb_serial_jtag_read_bytes(&c, 1, portMAX_DELAY) != 1) continue;
        if (c == '\r' || c == '\n') {
            if (len) { line[len] = 0; handle(line); len = 0; }
        } else if (len < sizeof line - 1) {
            line[len++] = (char)c;
        }
    }
}
