# ha_broan: Broan AI Series ERV wire protocol over RS-485 (ADR-0041). NOT Modbus — proprietary framing,
# token-passed half-duplex, 8-bit sum checksum. ha_broan_frame.c is the PURE codec (no deps, host-tested
# via test/run.sh); the transport-bound state machine layers on top of ha_rs485.
idf_component_register(SRCS "ha_broan_frame.c"
                       INCLUDE_DIRS "include")
