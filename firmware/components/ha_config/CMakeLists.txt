# ha_config: shared node-config loader (ADR-0020) — compile-time defaults (passed in by app_main from the
# board-local secrets.h) then NVS overlay. Secrets stay board-local (the seam), so this component only
# needs nvs_flash; it never includes secrets.h.
# mbedtls + esp_hw_support are for ADR-0036 Layer 0 only (HMAC-SHA256 over esp_fill_random + the base MAC
# to mint a node-born command secret). Still secrets-free: it GENERATES a secret, it never embeds one.
idf_component_register(SRCS "ha_config.c"
                       INCLUDE_DIRS "include"
                       REQUIRES nvs_flash mbedtls esp_hw_support)
