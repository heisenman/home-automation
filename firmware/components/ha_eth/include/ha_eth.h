#pragma once
#include "esp_err.h"
// Bring up the W5500 SPI Ethernet link and block until an IPv4 address is obtained via DHCP
// (or timeout_ms elapses). Drop-in replacement for ha_wifi_connect() on the wired S3-ETH node.
esp_err_t ha_eth_connect(int timeout_ms);
