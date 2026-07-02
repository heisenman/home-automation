# Mesh reach census (ADR-0023) — per-MAC RSSI-EWMA table + on-demand snapshot publish. Pure/leaf:
# holds only the table + a fallback task and takes a publish callback, so it pulls in neither ha_mqtt
# (edge) nor ha_relay. Uses only common IDF services (freertos, esp_log via esp_log_timestamp).
idf_component_register(SRCS "ha_reach.c"
                       INCLUDE_DIRS "include")
