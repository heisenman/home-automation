# Versioned, loadable battery profile (ADR-0024 §5): state offsets + V->SoC LUT + safety floors as
# DATA, not compiled constants. Pure core (ha_battery_profile.c — no ESP deps, host-tested in test/).
# Load/save runtime (NVS/SD/MQTT) is added as ha_battery_profile_rt.c at gauge-integration time.
idf_component_register(SRCS "ha_battery_profile.c"
                       INCLUDE_DIRS "include")
