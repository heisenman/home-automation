# ha_rs485: half-duplex RS-485 transport (ADR-0041). Two halves on purpose:
#   ha_rs485_timing.c  pure arithmetic (char time, TX duration, turnaround, t3.5) — host-tested, test/run.sh
#   ha_rs485.c         thin IDF UART glue: auto-direction OR hardware DE/RE via UART_MODE_RS485_HALF_DUPLEX,
#                      a transport-level listen-only gate, and optional echo suppression.
# Protocol-agnostic: ha_broan sits on top and nothing Broan-specific lives here.
idf_component_register(SRCS "ha_rs485.c" "ha_rs485_timing.c"
                       INCLUDE_DIRS "include"
                       REQUIRES driver
                       PRIV_REQUIRES esp_driver_uart esp_driver_gpio)
