# ha_sntp: shared best-effort clock sync (SNTP) — blocking initial sync + periodic re-sync + ISO-8601 UTC
# helper. No board seam (byte-identical across c3/c6/s3-eth). esp_netif provides esp_netif_sntp; time/log are
# common. The periodic re-sync matters because the RC-oscillator RTC drifts fast enough to fail the command
# freshness check (a clock-lockout that would even block the OTA that fixes it).
idf_component_register(SRCS "ha_sntp.c"
                       INCLUDE_DIRS "include"
                       REQUIRES esp_netif)
