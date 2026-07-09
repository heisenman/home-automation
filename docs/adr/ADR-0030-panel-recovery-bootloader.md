# ADR-0030 — Button-held bootloader recovery to a known-good panel firmware

**Date:** 2026-07-09
**Status:** **Accepted & implemented on the D1001** (2026-07-08) — Verification A + B and the immutable golden
partition are all proven on-device and captured to file (see `docs/design/panel-recovery-findings.md`).
E1001 port is TODO. Follows the D1001 near-brick during an E1001 flash mix-up (esptool reset the wrong panel
into the bootloader; only a battery-drain + reset recovered it) and the standing gap noted in `bsp_display.h:8`
— *"bootloader rollback isn't enabled yet."* Complements **[[ADR-0019]]** (panels are thin renderers) and the
open `ota-verify-window-rollback` hardening.

## Implementation as-built (D1001, 2026-07-08) — deviations from the original design below

The as-built design matches the intent but differs in specifics; the design sections further down are kept for
history. What actually shipped and was proven:
- **Button polarity is ACTIVE-HIGH (held = GPIO3 level 1)** — the opposite of the "active-low" assumption in
  the design below and in the old code comments. Verified (released=0/held=1), single-sourced in
  `main/recovery.h`. This inversion was the root cause of an early golden-boot loop.
- **Mechanism = a full 2nd-stage bootloader OVERRIDE, not a hook.** Hooks (`bootloader_after_init`) cannot
  redirect boot selection, so the impl is `bootloader_components/main/bootloader_start.c` (verbatim from the
  IDF `custom_bootloader/bootloader_override` example) with one change in `select_partition_number()`.
- **Trigger = a 3 s high-hold** via the official `bootloader_common_check_long_hold_gpio_level(GPIO3, 3, high)`
  (not a ~1 s debounce). A released pin returns instantly → zero normal-boot delay.
- **Golden = an immutable `test`-subtype partition** (`golden,app,test,0xA20000,4M`), booted by
  `TEST_APP_INDEX`. `test` (not an `ota_N` slot) is provably outside the OTA rotation, so a normal OTA can
  never clobber it, and it is never booted by default. Recovery is **transient** (no otadata write): release +
  reboot auto-returns to the normal app.
- **An app-level gate (A2) was built then RETIRED** — `esp_ota_set_boot_partition` only accepts `ota_N`
  partitions, incompatible with an immutable `test` golden, and the bootloader path is strictly more robust
  (covers crash-at-boot too). Recovery is now **bootloader-only**.
- **Prereqs verified:** secure boot + flash encryption are OFF → the custom bootloader needs no signing and a
  bad flash is ROM-download-mode + JTAG recoverable. 0xA20000..0xE20000 was flash-health surveyed before
  blessing golden.
- **Provenance caveat:** the golden binary bakes in `secrets.h` creds → archive it off-repo, never in the
  public repo. A golden build-from-source should be documented.

## Context — a stuck panel has no dead-simple physical recovery

Both wall panels can end up unrecoverable-without-a-computer:
- a **hung app** (blank screen, still powered — worse on the D1001, which has a battery + BQ25616 charger, so
  unplugging USB doesn't even power it off);
- a **bad OTA** that boots but misbehaves (the `ota-verify-window-rollback` gremlin: mark-valid is gated on
  MQTT-connect, so a slow first boot can miss the verify window);
- a **crash-at-boot** image that never reaches the app's own button read.

Today recovery means an esptool rescue over USB — which this session proved is fragile (the S3's native USB
self-disables ~8 s post-boot; the D1001 got reset by a mis-targeted flash). Hugh's requirement: **a physical
button that boots a known-good firmware, surviving even a crash-looping image** — better than a blind reset,
which just relaunches whatever broke.

## Decision — a custom 2nd-stage bootloader hook boots the known-good image when the button is held

Both panels build on **ESP-IDF** (the D1001 natively; the E1001 via ESPHome's `esp-idf` framework), so one
mechanism covers both: ESP-IDF's **bootloader hooks** (`bootloader_hooks.h` → `bootloader_before_init` /
`bootloader_after_init`), per the official `custom_bootloader/bootloader_multiboot` example. The hook runs
**before the app**, so it recovers even an app that crash-loops.

**Logic (in the 2nd-stage bootloader, before app load):**
1. Configure the **recovery button GPIO (GPIO3 on both panels** — D1001 back-button `bsp_display.h:65`, E1001
   green `e1001.yaml:9`) as an input with the appropriate pull.
2. If held (debounced, ≥ ~1 s) → **select the known-good partition** as the boot target and continue boot.
3. Otherwise → normal boot (selected OTA slot).

**Known-good target = a pinned GOLDEN partition where flash allows; else the other OTA slot:**
- **D1001 (P4, 32 MB flash):** `ota_0`/`ota_1` (4 MB each) end by ~10.6 MB → **~21 MB free**. Add a **`golden`
  4 MB app partition**. Golden is **statically blessed** — flashed once at provisioning with a known-good
  build and **never auto-updated**, so no bad OTA can ever corrupt it. Re-blessing is a deliberate manual op.
- **E1001 (S3, 32 MB chip but ESPHome set to `flash_size: 8MB`):** two ~3.9 MB slots ≈ fill 8 MB → no golden
  room unless we enable the real 32 MB flash (ESPHome `flash_size` + esp-idf >16 MB-OTA experimental flag).
  Phase-2 decision: enable 32 MB for a golden (recommended — chip has it), or fall back to **other-slot**.

**UX:** the golden build renders a persistent **"RECOVERY — known-good firmware"** banner so the user knows
they're on golden and how to leave it (OTA a good build / power-cycle without the button).

## Why this is safe to attempt (the brick math)

A custom 2nd-stage bootloader is the one firmware surface that can *hard*-brick — but the backstops are layered:
1. **ROM download mode is un-brickable** — the 1st-stage ROM loader can't be overwritten; BOOT-held → esptool
   reflashes a bad 2nd-stage bootloader.
2. **Hardware backstop** — Hugh has JTAG programmers + physical access ("a screwdriver"); worst case, reflash
   the bootloader on the bench.
3. **Staged rollout** — prove on a bench panel with a rescue staged before any wall panel; never blind-OTA a
   bootloader to a mounted panel first.

## Phased plan (accepted)

| Phase | Scope | Flash risk |
|---|---|---|
| **0** | This ADR — partition layout, hook design, button gesture, recovery UX. | none |
| **1** | **D1001 first** (ESP-IDF-native, USB-JTAG recovery, flash room): add `golden` partition + a
`bootloader_components/` hook reading GPIO3 → boot golden. Build, then bench-flash with download-mode + JTAG
rescue staged; prove hold-button → golden and normal-boot → app. | bootloader flash, rescue ready |
| **2** | Port to E1001 (ESPHome `esp-idf` bootloader component). Decide golden (enable 32 MB flash) vs
other-slot. NB: E1001 deploy is *separately* blocked on the USB-window / MQTT-reboot flash path. | after Phase 1 proven |

## Open / to confirm (docs-first)

- **P4 GPIO3 strapping** — confirm from the ESP32-P4 datasheet / D1001 schematic that holding GPIO3 at reset is
  benign (S3 GPIO3 = JTAG-source strap, benign; the button is already wired to GPIO3 in hardware on both, so
  this is a config concern, not a wiring change). **Hugh (EE) authoritative.**
- **Golden population/re-bless** workflow (provisioning step + a deliberate "bless current as golden" op).
- Interplay with **A/B mark-valid** (`ota-verify-window-rollback`): golden is the manual escape; proper
  mark-valid auto-rollback is a complementary follow-up.
- ESPHome bootloader-component injection specifics (Phase 2).

## Consequences

- **+** A dead-simple, crash-loop-proof physical recovery on both panels; closes the `bsp_display.h:8` gap.
- **+** Golden is immutable → a guaranteed-good baseline no OTA can poison.
- **−** Custom bootloader on both panels (build complexity, esp. ESPHome injection); bootloader changes need a
  **full flash** (not app-OTA), so they land at provisioning / on the bench, not via the normal OTA path.
- **−** Golden costs a flash partition (trivial on D1001; needs the 32 MB-flash enable on E1001).
