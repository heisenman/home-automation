# ha_gaposa: Gaposa QCTZ36SDU shade-panel command planner (ADR-0041). Enforces the panel's per-channel AND
# box-wide interlocks (mixed commands across channels latch an error state), bounds every pulse well under
# the 30 s lockout, and models position open-loop from travel time. Pure decision logic — no GPIO, no deps
# — so it is unit-testable on the host (test/run.sh). Pair the outputs with ha_dout.
idf_component_register(SRCS "ha_gaposa.c"
                       INCLUDE_DIRS "include")
