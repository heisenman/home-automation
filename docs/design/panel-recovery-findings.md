# Panel recovery (ADR-0030) — running findings log

Living log of tests, failures, and findings while building the button-held → known-good-firmware recovery.
Newest at top. Failures are kept on purpose (Hugh: "document everything including failures and findings").

## 2026-07-08 (D) — demonstrable power-off + golden recovery across a REAL power cycle

**Context:** the D1001 has a battery + BQ25616 charger, so USB-unplug alone doesn't power it off. Full
firmware shutdown = `bsp_power_off()` releases the `PWR_HOLD` latch (PCA9535 P8) → VDD_3V3 collapses (on
battery). Power ON = physical side button (hardware CJ3407 latch, not firmware-reachable) or plugging USB.
**On USB the rail is held up by VBUS**, so a true power-off can only be *observed* on battery. Can't probe the
rail without disassembly — so we proved it by **signal, not scope**.

**Method (no disassembly):** serial-node presence = P4 powered (USB-JTAG is inside the P4, off VDD_3V3); plus
a timestamped MQTT status poll on `.210` (LWT + `uptime_s`). Baseline: serial present, `online ota_1
uptime 634`. Hugh did the force-off gesture (button + USB unplug), then re-plugged.

**Result (MQTT timeline — `scratchpad/poweroff_mqtt.log`):**
```
online ota_1  uptime 634   -> OFFLINE (LWT)  -> online GOLDEN uptime 4  -> OFFLINE -> online ota_1 uptime 3
```
- **Force-off is real:** LWT `offline` fired AND `uptime_s` reset 634→4. A dark-but-running device cannot
  reset uptime — it genuinely power-cycled. (Confirmed by Hugh.)
- **USB-plug auto-powers-on** (matches the BSP "power on = plug USB") — so there is no USB-plugged-but-off
  state; the cold-boot signature is the proof.
- **Golden recovery survives a TRUE power cycle:** the first cold boot came up on `partition:golden` — GPIO3
  held ~3 s across the USB-plug cold boot tripped the bootloader recovery. So recovery works from a real
  rail-down power-on, not just a warm `panel.sh reset`. (Hugh reproduced it deliberately.)

**Note / open UX question:** the power-on-via-USB cold boot passes through the recovery gate, so holding the
green button during re-power lands in golden. Whether the "power" gesture and the GPIO3 "recovery" gesture are
the same physical button or separate (side button vs green back button) is a schematic detail to confirm; if
separate they're independent, if overlapping it's a deliberate-hold UX note. Not a defect.

## 2026-07-08 (C) — IMMUTABLE golden partition + bootloader-only recovery (A2 retired)

**Goal:** make golden un-clobberable by a normal OTA and remove the redundant app-level path.

**Decision (Hugh):** bootloader-only recovery. Golden moves to a dedicated **`test`-subtype** app partition;
the app-level A2 gate is retired (it switched via `esp_ota_set_boot_partition`, which only accepts `ota_N`
partitions — incompatible with a non-ota golden, and B1 already covers everything A2 did, more robustly).

**Why `test` works (verified from `bootloader_utility.c`):** `esp_ota_get_next_update_partition` only cycles
`ota_N` slots → a `test` partition is **never** an OTA target; `get_selected_boot_partition` only returns the
otadata-selected ota slot → golden is **never** booted by default; `load_boot_image(bs, TEST_APP_INDEX)` loads
`bs->test` directly → the bootloader can boot it explicitly. All three properties hold simultaneously.

**Flash-health survey first (Hugh asked; the region was unproven):** full 32 MB chunked read
(`scratchpad/flash_survey.sh` → `flashdump/results.log`) — **all 16×2 MB chunks read OK**, no FAIL/TIMEOUT;
the historically-flagged 0x600000 sector read OK this pass (it's routed around regardless). **Surprise:** the
"free" golden target (0xA20000..~0xCC0000) was **not blank** — it held **orphaned audio/factory assets**
(Adobe BWF/XMP + PCM), not referenced by any partition. Surfaced to Hugh (don't erase unidentified data);
he OK'd erasing it (also backed up in the survey dump). Its being written confirms the region is *writable*.

**Placement:** read golden off ota_1 (0x620000), cut a clean image at the exact end (0x227630 bytes,
image_info hash-valid → `golden_clean.bin`). New table row `golden, app, test, 0xA20000, 0x400000`
(ota_0/ota_1 unchanged). Bootloader override now returns `TEST_APP_INDEX` on the 3 s hold. Erased the golden
region, wrote `golden_clean.bin`, **read back → sha MATCH** (`bee6e942…`) — pristine, verified on-flash.

**Proven on-device (app `v104-golden-part`, captured):**
| Button | Result |
|---|---|
| released | bootloader enumerates `golden test app 0xa20000`; boots ota_0/v104; no recovery line; clean. |
| held 3 s | `ADR-0030 RECOVERY: gpio3 held 3s -> booting GOLDEN (test @0xa20000)` → load **0xa20000** → `RECOVERY(v98-GOLDEN): running=golden` + red LED. |
| release+reset | auto-returns to v104/ota_0 (transient). |

**Immutability** is guaranteed by ESP-IDF design (test ∉ OTA rotation; table has exactly ota_0/ota_1 as the
`ota_N` slots). **Provenance gap to close:** the golden *binary* (v98-GOLDEN) is placed on-device from a dump;
it is NOT in the repo, so a from-scratch reflash needs it archived/documented. Empirical OTA-survives-golden
demo optional.

## 2026-07-08 (B) — Verification B PROVEN: 2nd-stage bootloader override (crash-boot coverage)

**Goal:** cover the case the app-level gate (A2) cannot — a *bad/crash boot* where the app never runs, so
only the 2nd-stage bootloader can divert to golden. Long-press (3 s, confirmed w/ Hugh) → golden.

**Config facts verified first (DOCS-FIRST):** `CONFIG_SECURE_BOOT` **off**, `CONFIG_SECURE_FLASH_ENC` **off**
→ a custom bootloader needs no signing and a bad flash is ROM-download-mode + JTAG recoverable. Golden =
**ota_1 = boot index 1** (partitions csv: ota_0@0x10000, ota_1@0x620000). `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`.

**Primitive (official IDF API, not hand-rolled):** `bootloader_common_check_long_hold_gpio_level(pin, delay_sec,
level)` — enables the **internal pull-up** (same config polarity was verified under), returns immediately if the
pin is NOT at `level` (so a released boot pays **zero** delay), else `GPIO_LONG_HOLD` after `delay_sec`. With
`level=true` this is our active-HIGH long-press exactly.

**Mechanism:** ESP-IDF `bootloader_components/` in the project. `recovery.h` is single-sourced into the
bootloader via `INCLUDE_DIRS`.
- **B0 (read-only hook, `bootloader_after_init`)** — prints the GPIO3 classification only; cannot redirect
  boot (safest first bootloader flash). CONFIRMED: released→`NOT_HOLD`, held→`LONG_HOLD`, printed at ~55 ms
  (bootloader stage, ~360 ms before the app loads). **Bootloader-context read matches the app read.**
- **B1 (full override, `bootloader_components/main/bootloader_start.c`)** — verbatim from the IDF
  `custom_bootloader/bootloader_override` example; the ONLY change is in `select_partition_number()`: on a
  3 s GPIO3 high-hold, return the golden index instead of `get_selected_boot_partition()`. Never writes otadata.

**B1 results (captured):**
| Button | Captured behaviour |
|---|---|
| released | no RECOVERY line; boot ota_0@0x10000 at ~406 ms; **zero delay**; normal v101. |
| held 3 s | `[boot] ADR-0030 RECOVERY: gpio3 held 3s -> booting GOLDEN (ota_1)` → **direct** load of 0x620000 at ~3401 ms → `GOLDEN RECOVERY IMAGE ACTIVE` + red LED. **No ota_0 load, no app A2 line** — the bootloader diverted before the app ran. |
| release + reset | auto-returns to v101/ota_0 (transient — no otadata write, no otatool flip needed). |

**Safety property:** a released boot takes the exact stock path (`get_selected_boot_partition`); only a held
boot hits new code — so a bug in the held path cannot break normal boots. Secure boot off = recoverable.

**Gotcha logged:** adding/removing a `bootloader_components/` component requires `idf.py fullclean` — a plain
incremental build does NOT re-scan the bootloader subproject, so the first B0 build silently produced a STOCK
bootloader (verified via `nm bootloader.elf`: `bootloader_hooks_include` = `U`). Always `nm` the bootloader
elf to confirm the override/hook linked *before* flashing.

**Bootloader size:** override = 0x5cb0, 0x350 (3%) free — fits, tight. Watch this if the hook grows.

**B1c — crash-recovery capstone PROVEN (the exact ADR-0030 scenario):** temporarily replaced `app_main`'s
first line with `abort()` (uncommitted; reverted via `git checkout` after), tag `v103-crashtest`, flashed to
ota_0. Captured a **stable boot-loop**: `boot: Loaded app 0x10000` → `[CRASHTEST v103] deliberate abort` →
`Rebooting` → `SW_CPU_RESET` → repeat every ~2 s, forever (a serially-flashed app stays valid → no
auto-rollback; the device cannot self-recover). Then a **3 s button hold** while looping →
`[boot] ADR-0030 RECOVERY: gpio3 held 3s -> booting GOLDEN (ota_1)` → direct load of 0x620000 →
`GOLDEN RECOVERY IMAGE ACTIVE` + red LED. **The crashing app never ran** (no CRASHTEST line in the recovery
capture — the bootloader diverted before it). Restored: reverted the edit, rebuilt + reflashed v101 to ota_0,
confirmed normal boot. `PANIC_PRINT_REBOOT=y`/0 s delay makes the loop fast+stable.

**Verification B COMPLETE.** Device: ota_0=v101-recovery-a2, ota_1=v98-GOLDEN, B1 override bootloader.
Remaining ADR-0030 work: a real **immutable/sticky golden partition** (today golden reuses ota_1, which OTA
ping-pongs; and app-A2 golden is one-shot). The bootloader path (B1) is already non-sticky-by-design and
correct; the app-A2 path and the partition layout are the parts left to harden.

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
