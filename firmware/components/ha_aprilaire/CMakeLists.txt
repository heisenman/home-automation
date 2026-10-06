# ha_aprilaire: Aprilaire E070 Remote (Model 76) RS-485 frame codec (ADR-0041). Pure C — host-tested,
# test/run.sh. The UART/direction control is ha_rs485's job; this only encodes/decodes frames.
idf_component_register(SRCS "ha_aprilaire.c"
                       INCLUDE_DIRS "include")
