# Panel recovery (ADR-0030) — running findings log

Living log of tests, failures, and findings while building the button-held → known-good-firmware recovery.
Newest at top. Failures are kept on purpose (Hugh: "document everything including failures and findings").

## 2026-07-08 — Phase 1a: GPIO3 boot-read probe on the D1001 (P4)

**Goal:** before touching the bootloader (brick-capable), prove at the *app* layer that (a) the recovery
button (GPIO3) is readable at the earliest boot point and (b) holding it at reset is benign on the P4
(i.e. GPIO3 is not a boot-mode strapping pin). Hugh's method — read the pin, hold it, watch serial.

**Method:** `esp_rom_printf` probe as the first statement in `app_main` (`beachhead_main.c`), reading
`gpio_get_level(GPIO_NUM_3)` after configuring it input+pull-up. Build `v97-recoveryprobe`, OTA over wifi
(no serial-port writes — safe), watch serial (read-only `panel.sh console`). D1001 only device on USB.

**Result — CONFIRMED:**
| Attempt | Button | Probe read | Booted? |
|---|---|---|---|
| 1 | held (mistimed) | `1` (released) | yes, v97 |
| 2 | held continuously | **`0` (HELD)** | **yes, v97** |

- GPIO3 active-low + pull-up confirmed (held=0, released=1).
- **Holding GPIO3 LOW at reset is BENIGN on the ESP32-P4** — device reached `app_main` and booted normally.
  GPIO3 is not a boot-blocking strap. (Empirical answer; the ESP32-P4 datasheet WebFetch failed — see below.)
- **Booting from ota_1 (second slot) works** — both OTAs landed in ota_1/ota_0 and booted cleanly. Good
  signal for the golden/other-slot boot target.

**Failure noted (attempt 1):** the probe fires at app entry **~2 s after reboot**, but the display comes on
~6–8 s later. Holding "until the display came on" started *after* the 2 s window → read released. Fix for the
real feature: **sample the button over a short window** (e.g. poll for the first ~1–2 s), don't trust a
single instant. Re-test with a continuous hold read `0` correctly.

**Tooling failure noted:** `WebFetch` of the ESP32-P4 datasheet PDF returned `400 adaptive thinking is not
supported on this model` (both the espressif.com URL and its `documentation.espressif.com` redirect; the PDF
did download locally to the tool-results dir). Pivoted to the empirical read above, which is more direct.

**Next (Phase 1a cont.):** add the app-level recovery *action* — held-at-boot (windowed) →
`esp_ota_set_boot_partition(known-good)` + `esp_restart()` — with a visibly distinct golden/recovery image,
and prove the full "hold → boots known-good" flow at the app layer. Then port down into the bootloader hook
(Phase 1b) for crash-at-boot coverage.

## 2026-07-08 — the incident that motivated ADR-0030

During an E1001 flash attempt, esptool was pointed at `/dev/ttyACM0` believing it was the E1001 (S3). It was
the **D1001 (P4)**; esptool's chip-ID guard aborted **before any write** (no flash, no erase), but the
`--before default-reset` had already reset the P4 into the bootloader, and the run errored before the
`--after hard-reset` that boots the app. The D1001 has a battery + BQ25616 charger, so unplugging USB didn't
power it off — it sat in the bootloader with a blank screen ("bricked"-looking). Recovered by a full reset
(replug → boot chime). **No firmware was ever written.** Lessons: (1) `esptool chip-id` FIRST, always;
(2) a battery-backed panel isn't power-cycled by a USB unplug; (3) this is exactly the "no dead-simple
physical recovery" gap ADR-0030 closes.
