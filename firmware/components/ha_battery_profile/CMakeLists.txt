# Versioned, loadable battery profile (ADR-0024 §5): state offsets + V->SoC LUT + safety floors as
# DATA, not compiled constants. Pure core (ha_battery_profile.c — no ESP deps, host-tested in test/).
# Runtime side (ha_battery_profile_rt.c) adds NVS persistence + JSON transport; it produces/consumes
# validated structs only (no ha_battery dep), so the caller wires an accepted profile into the gauge.
idf_component_register(SRCS "ha_battery_profile.c" "ha_battery_profile_rt.c"
                       INCLUDE_DIRS "include"
                       REQUIRES nvs_flash json)
