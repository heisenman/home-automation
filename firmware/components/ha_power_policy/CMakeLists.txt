# Battery low-power SAFETY policy (ADR-0024): hard-shutdown / warn / boot-gate decisions on
# millivolt thresholds, board-agnostic via injected actuators. Split into a pure decision core
# (ha_power_policy.c — no ESP deps, host-tested in test/) and a thin runtime (ha_power_policy_rt.c).
idf_component_register(SRCS "ha_power_policy.c" "ha_power_policy_rt.c"
                       INCLUDE_DIRS "include"
                       REQUIRES freertos)
