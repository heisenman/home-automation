# ha_dout: shared dry-contact / relay OUTPUT driver (ADR-0041) — fail-safe level, short-cycle dwell,
# hard max-on watchdog, momentary pulses. Pure decision logic: no GPIO, no ESP deps, so it needs no
# REQUIRES at all and is unit-testable on the host (test/run.sh). The caller applies ha_dout_level().
idf_component_register(SRCS "ha_dout.c"
                       INCLUDE_DIRS "include")
