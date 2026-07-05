# ha_relay: shared Phase-B relay coverage filter (ADR-0015) — signed relay_assign, NVS-persisted,
# epoch-guarded allowlist. No board seam (byte-identical across c3/c6/s3-eth), so it's a plain promote:
# every edge node links the same filter. cJSON (json) parses the directive; nvs_flash persists it.
idf_component_register(SRCS "ha_relay.c"
                       INCLUDE_DIRS "include"
                       REQUIRES nvs_flash json)
