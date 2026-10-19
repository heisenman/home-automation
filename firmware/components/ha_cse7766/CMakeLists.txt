# ha_cse7766: CSE7766 single-phase power-meter frame parser. Pure C — host-tested, test/run.sh. The UART
# (4800 8E1, RX only) is the caller's.
idf_component_register(SRCS "ha_cse7766.c"
                       INCLUDE_DIRS "include")
