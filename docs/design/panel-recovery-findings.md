# Panel recovery (ADR-0030) — running findings log

Living log of tests, failures, and findings while building the button-held → known-good-firmware recovery.
Newest at top. Failures are kept on purpose (Hugh: "document everything including failures and findings").

## 2026-07-08 (later) — polarity CORRECTED to active-HIGH; Verification A PROVEN (app-level)

**⚠️ CORRECTION of the Phase-1a conclusion below.** The Phase-1a table concluded "GPIO3 active-LOW
(held=0, released=1)." **That is WRONG.** Every recovery build that assumed it (v97–v99) false-switched to
golden on a normal, released boot — because a released pin reads **0**, which those builds treated as "held."
The Phase-1a reads were mistimed (attempt 1 was noted as such; attempt 2 was as well). Re-verified
methodically, captured to file, repeatable:

**GPIO3 is ACTIVE-HIGH: released = 0, held = 1.** Verified two independent ways —
1. **Runtime** signed `d1001_cmd.py gpio 3` (pin already input+pull-up via `button_task`): released→`0`,
   held→`1`. Held reads 1 even against the internal pull-up.
2. **Boot-time** early in `app_main` (same input+pull-up config), fast-poll serial capture: released→`0`,
   held→`1`, identical to runtime.

Single source of truth is now `main/recovery.h` (`RECOVERY_BUTTON_GPIO 3`, `RECOVERY_BUTTON_ACTIVE_LVL 1`),
shared by the app now and the bootloader hook later. Root cause of the golden-boot loop = a **polarity
inversion**, provable in two reads. See [[feedback-dont-overrun-proof]].

**Capture method (what finally worked reliably):** `scratchpad/fastcap.py` — a fast-poll (~15 ms) serial
reader with DTR/RTS deasserted. Started immediately after `panel.sh reset`, it re-grabs the USB-JTAG port the
instant the reset releases it and catches boot from ROM onward — so the early (~1.8 s) recovery line is never
missed. Every result below is captured to a `scratchpad/*.log` file and examined after (never read live).

**A1 — read-only boot gate (`v100-recovery-a1`), no action:**
| Button | Early-boot read (`app_main`) | Matches runtime? |
|---|---|---|
| released | `gpio3=0 held=0` | ✅ (runtime 0) |
| held | `gpio3=1 held=1` | ✅ (runtime 1) |

Closes the one open unknown: the **boot-time read (reset-default pin state) matches the runtime read**. Same
log line, same ~1827 ms timing, only the level differs.

**A2 — golden-switch action (`v101-recovery-a2`), one-shot:** boot gate reads GPIO3 early (before wifi/display),
glitch-rejects (3 samples, ~12 ms apart, all must be 1), then `esp_ota_set_boot_partition(ota_1)` + `esp_restart()`.
| Button | Result (captured) |
|---|---|
| released | `RECOVERY boot-gate: gpio3=0 held=0` → **normal boot**, v101 from ota_0. No false-switch (the fixed bug). |
| held | `held=1` → `booting golden (ota_1 @0x620000); restarting` → boot from **0x620000** → `GOLDEN RECOVERY IMAGE ACTIVE` + red LED on. |

**Verification A (hold-at-boot → golden) is DONE at the app layer**, both the safety case (released ≠ switch)
and the switch case (held → golden). Restored to v101/ota_0 afterward via `otatool switch_ota_partition
--slot 0` (edits otadata only; golden untouched). Device state: **ota_0 = v101-recovery-a2** (running),
**ota_1 = v98-GOLDEN** (intact).

**One-shot caveat (by design, confirmed with Hugh):** golden (v98) never MQTT-connects → never marks itself
valid → with OTA rollback enabled the bootloader returns to ota_0 on the next reboot. Sticky/immutable golden
is the separate follow-up (immutable golden partition).

**Still NOT done (needs the bootloader hook — Phase 1b, brick-capable):** Verification B = a *bad/crash boot*
then a long press → golden. Only the 2nd-stage bootloader runs before a crashing app, so the app-level gate
above cannot cover it. Do not start B without an explicit go — it edits the bootloader.

**Pre-existing bug flagged (separate from recovery):** the screen-toggle `button_task` in `beachhead_main.c`
(~L257–274) has the SAME inverted polarity — `prev=1` released, treats `lvl==0` as a press. Not fixed in
this pass; awaiting Hugh's call on scope.

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
