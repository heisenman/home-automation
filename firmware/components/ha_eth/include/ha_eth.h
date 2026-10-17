// BREADCRUMB: firmware/components > ha_eth - W5500 SPI Ethernet bring-up that blocks until DHCP gives an IPv4 address; drop-in for ha_wifi_connect on wired nodes. Contract: ADR-0020. Parent: firmware/AGENTS.md.
// REUSE-WHEN: a node has wired Ethernet (W5500, e.g. the S3-ETH) and needs the network up before MQTT — the wired twin of ha_wifi.
#pragma once
#include "esp_err.h"
// Bring up the W5500 SPI Ethernet link and block until an IPv4 address is obtained via DHCP
// (or timeout_ms elapses). Drop-in replacement for ha_wifi_connect() on the wired S3-ETH node.
esp_err_t ha_eth_connect(int timeout_ms);
