# W5500 SPI-Ethernet bring-up (S3-ETH platform module, ADR-0020). Single-consumer today but catalog-tracked
# as a real component so a future wired board is a column, not a fork.
#
# The ESP32-P4 panel (beachhead) auto-discovers this component via its whole-firmware/components
# EXTRA_COMPONENT_DIRS and has NO wired NIC + no W5500 driver dependency, so compiling ha_eth.c there fails
# (esp_eth_phy_new_w5500 undeclared). It has no ethernet hardware, so register an EMPTY component on P4 and
# leave the edge (esp32s3-eth, esp32c6) builds exactly as they were.
if(IDF_TARGET STREQUAL "esp32p4")
    idf_component_register(INCLUDE_DIRS "include")
else()
    idf_component_register(SRCS "ha_eth.c"
                           INCLUDE_DIRS "include"
                           REQUIRES driver esp_eth esp_event esp_netif esp_hw_support)
endif()
