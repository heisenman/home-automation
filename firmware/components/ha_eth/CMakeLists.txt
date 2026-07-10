# W5500 SPI-Ethernet bring-up (S3-ETH platform module, ADR-0020). Single-consumer today but catalog-tracked
# as a real component so a future wired board is a column, not a fork.
idf_component_register(SRCS "ha_eth.c"
                       INCLUDE_DIRS "include"
                       REQUIRES driver esp_eth esp_event esp_netif esp_hw_support)
