# ha_broan: Broan AI Series ERV over RS-485 (ADR-0041). NOT Modbus — proprietary framing, token-passed
# half-duplex, 8-bit sum checksum. Two layers, both PURE (no deps, host-tested via test/run.sh):
#   ha_broan_frame.c  the wire codec — framing, checksum, register encode/decode, TLV walking
#   ha_broan.c        the session state machine — token handshake, ping/pong, heartbeat, chunked polling
# Neither touches a UART: ha_rs485 drives this, which is what keeps the conversation provable off-target.
idf_component_register(SRCS "ha_broan_frame.c" "ha_broan.c"
                       INCLUDE_DIRS "include")
