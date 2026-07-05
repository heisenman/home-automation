# Signed-directive verification (ADR-0010) — shared HMAC-SHA256 {p,s} envelope verifier with freshness +
# NVS (ts,seq) anti-replay. Ported from the live edge-node verifier so a second device enforces the same
# scheme. Leaf: depends only on mbedtls (HMAC), nvs_flash (replay high-water), json (cJSON).
idf_component_register(SRCS "ha_cmd.c"
                       INCLUDE_DIRS "include"
                       REQUIRES mbedtls nvs_flash json)
