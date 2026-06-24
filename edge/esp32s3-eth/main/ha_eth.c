// W5500 SPI Ethernet bring-up for the ESP32-S3-ETH edge node. The S3 has no internal Ethernet MAC,
// so the board drives an external W5500 over SPI. Mirrors ha_wifi.c's contract: init netif/events,
// start the link, block until DHCP yields an IP. No Wi-Fi/BLE radio coexistence to worry about — the
// wire is independent of the 2.4 GHz radio the BLE scanner uses.
#include "ha_eth.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "driver/spi_master.h"
#include "driver/gpio.h"
#include "esp_eth.h"
#include "esp_event.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_log.h"

static const char *TAG = "ha_eth";

// ── Waveshare ESP32-S3-ETH (standard board) → W5500 wiring ──────────────────────────────────
//   Confirmed pinout for the standard "ESP32-S3-ETH". The industrial 8DI-8DO/8RO variants wire
//   the W5500 differently (CLK15 / MOSI13 / MISO14 / CS16 / INT12 / RST39) — change these six if so.
#define ETH_SPI_HOST     SPI2_HOST
#define ETH_PIN_MOSI     11
#define ETH_PIN_MISO     12
#define ETH_PIN_SCLK     13
#define ETH_PIN_CS       14
#define ETH_PIN_INT      10
#define ETH_PIN_RST       9
#define ETH_SPI_CLOCK_MHZ 20      // W5500 handles up to ~33-40 MHz; 20 is a conservative, reliable start

#define ETH_GOT_IP_BIT   BIT0
static EventGroupHandle_t s_eth_events;

static void on_eth_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (base == ETH_EVENT) {
        switch (id) {
            case ETHERNET_EVENT_CONNECTED:    ESP_LOGI(TAG, "link up");   break;
            case ETHERNET_EVENT_DISCONNECTED: ESP_LOGW(TAG, "link down"); break;
            case ETHERNET_EVENT_START:        ESP_LOGI(TAG, "eth start"); break;
            case ETHERNET_EVENT_STOP:         ESP_LOGI(TAG, "eth stop");  break;
            default: break;
        }
    }
}

static void on_got_ip(void *arg, esp_event_base_t base, int32_t id, void *data) {
    ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
    ESP_LOGI(TAG, "got ip " IPSTR, IP2STR(&e->ip_info.ip));
    xEventGroupSetBits(s_eth_events, ETH_GOT_IP_BIT);
}

esp_err_t ha_eth_connect(int timeout_ms) {
    s_eth_events = xEventGroupCreate();
    // esp_netif_init() + esp_event_loop_create_default() are done once in app_main (shared with Wi-Fi).

    esp_netif_config_t netif_cfg = ESP_NETIF_DEFAULT_ETH();
    esp_netif_t *eth_netif = esp_netif_new(&netif_cfg);

    // The W5500 driver hangs an ISR on its INT GPIO during esp_eth_start — the GPIO ISR service
    // must already exist, or that registration fails and interrupt-driven RX never fires (no DHCP).
    gpio_install_isr_service(0);   // idempotent; ESP_ERR_INVALID_STATE if already installed → ignore

    // SPI bus shared with the W5500 only.
    spi_bus_config_t buscfg = {
        .miso_io_num = ETH_PIN_MISO,
        .mosi_io_num = ETH_PIN_MOSI,
        .sclk_io_num = ETH_PIN_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
    };
    ESP_ERROR_CHECK(spi_bus_initialize(ETH_SPI_HOST, &buscfg, SPI_DMA_CH_AUTO));

    spi_device_interface_config_t devcfg = {
        .mode = 0,
        .clock_speed_hz = ETH_SPI_CLOCK_MHZ * 1000 * 1000,
        .queue_size = 20,
        .spics_io_num = ETH_PIN_CS,
    };
    eth_w5500_config_t w5500_cfg = ETH_W5500_DEFAULT_CONFIG(ETH_SPI_HOST, &devcfg);
    w5500_cfg.int_gpio_num = ETH_PIN_INT;

    eth_mac_config_t mac_cfg = ETH_MAC_DEFAULT_CONFIG();
    esp_eth_mac_t *mac = esp_eth_mac_new_w5500(&w5500_cfg, &mac_cfg);

    eth_phy_config_t phy_cfg = ETH_PHY_DEFAULT_CONFIG();
    phy_cfg.phy_addr = 1;                 // W5500 is a fixed single-PHY device
    phy_cfg.reset_gpio_num = ETH_PIN_RST;
    esp_eth_phy_t *phy = esp_eth_phy_new_w5500(&phy_cfg);

    esp_eth_config_t eth_cfg = ETH_DEFAULT_CONFIG(mac, phy);
    esp_eth_handle_t eth_handle = NULL;
    ESP_ERROR_CHECK(esp_eth_driver_install(&eth_cfg, &eth_handle));

    // The W5500 ships without a burned-in MAC — give it the chip's ETH-derived address.
    uint8_t mac_addr[6];
    ESP_ERROR_CHECK(esp_read_mac(mac_addr, ESP_MAC_ETH));
    ESP_ERROR_CHECK(esp_eth_ioctl(eth_handle, ETH_CMD_S_MAC_ADDR, mac_addr));

    ESP_ERROR_CHECK(esp_netif_attach(eth_netif, esp_eth_new_netif_glue(eth_handle)));
    ESP_ERROR_CHECK(esp_event_handler_register(ETH_EVENT, ESP_EVENT_ANY_ID, on_eth_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, on_got_ip, NULL));

    ESP_ERROR_CHECK(esp_eth_start(eth_handle));

    EventBits_t bits = xEventGroupWaitBits(s_eth_events, ETH_GOT_IP_BIT, pdFALSE, pdFALSE,
                                           pdMS_TO_TICKS(timeout_ms));
    return (bits & ETH_GOT_IP_BIT) ? ESP_OK : ESP_FAIL;
}
