# ha_config: shared node-config loader (ADR-0020) — compile-time defaults (passed in by app_main from the
# board-local secrets.h) then NVS overlay. Secrets stay board-local (the seam), so this component only
# needs nvs_flash; it never includes secrets.h.
idf_component_register(SRCS "ha_config.c"
                       INCLUDE_DIRS "include"
                       REQUIRES nvs_flash)
