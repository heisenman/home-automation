// recovery.h — D1001 panel recovery-to-golden button (ADR-0030).
//
// Single source of truth for the recovery button's pin + polarity, shared by BOTH
// the application (Verification A, app-level) and the eventual 2nd-stage bootloader
// hook (Verification B, ADR-0030 Phase 1b). The app and bootloader are separate
// binaries and read the pin with different APIs (driver/gpio.h vs esp_rom/register),
// so only the CONSTANTS below are shared; each context implements its own read against
// them. Keep those reads comparing to RECOVERY_BUTTON_ACTIVE_LVL — never a bare == 0/1.
//
// POLARITY IS VERIFIED, NOT ASSUMED (2026-07-08): read GPIO3 via the signed `gpio 3`
// command with the button released -> level 0, then held -> level 1. Held reads 1 even
// with the internal pull-up enabled. The button is ACTIVE-HIGH. The "active-low w/
// pull-up" comment elsewhere in beachhead_main.c is WRONG and was the root cause of the
// golden-boot loop (every prior recovery build treated released level 0 as "held").
// See docs/design/panel-recovery-findings.md and ADR-0030.
#pragma once

// Back-of-device recovery button.
#define RECOVERY_BUTTON_GPIO        3
// The pin level that means "button held". VERIFIED active-HIGH: held == 1.
#define RECOVERY_BUTTON_ACTIVE_LVL  1
