# D1001 battery-charge investigation (2026-07-02)

Parked deliberately — battery backup is a nice-to-have. Recorded in full because the *process*
(and its conclusion) is reusable engineering wisdom. Companion to
[reterminal-d1001.md](reterminal-d1001.md).

## TL;DR — conclusion
The D1001 bench unit does **not** visibly charge its cell (VBAT pinned ~3.83–3.86 V, STAT = not
charging) under **both** our beachhead firmware **and** the **verbatim Seeed factory charge code**.
Because the factory's own byte-identical code reproduces the non-charge — at *minimal* load, on a
*healthy* USB rail — **this is not a defect in our firmware.** It is hardware or hardware-state
(BQ25616, the cell, the board, or a persistent BQ latch). See "open confound" below.

## The definitive test (what licenses the conclusion)
Per the [[feedback-firmware-is-the-problem]] rule, we didn't get to blame hardware until the factory
code was run verbatim:
1. Built the Seeed **factory_firmware** (Brookesia). It **bootloops at ~1.8 s** — isolated to the
   **UI**, not the charge path (`bsp_power_init` runs fine).
2. Trimmed `app_main()` to **stop at `bsp_spiffs_mount()`** (drops codec/display/Brookesia; linker GC'd
   it → 7.1 MB → 753 KB). This runs **`bsp_power_init()` verbatim** = the *entire* factory charge setup
   (PMU DC-DC tweak → I²C/ADC → io_expander `0xffff` + `BAT_CHARGE_EN=0` + rails → `bsp_battery_manage_start`).
3. Added a passive `CHGMON` serial log (VBAT/USB/STAT/PG), zero behavioral change.
4. **Result:** `VBAT=3836mV USB=4798mV charging=0 STAT=1 PG=0`, flat for 40 s+, at minimal load
   (no display, no WiFi/C6). **Identical to our beachhead.** ⇒ the factory code reproduces it ⇒ not us.

## The one open confound (why it's not 100% "hardware")
The BQ25616 is a separate power domain that **survives P4 reboots**, so it may be **latched** from our
earlier `/CE` abuse (see history), and neither firmware resets it. *But:* even when we **did** un-latch
it (STAT→0 via a ~2 s `/CE`-high reset), **VBAT still did not rise**, and STAT re-latched within ~1–2 min.
So the latch is likely not the sole cause. Resolving this is the first "if resumed" step below.

## Firmware bugs we found and fixed along the way (all real, all ours — committed)
1. **A 15 s `/CE`-pulsing "watchdog"** (added earlier to "kick" a stalled charger) **latched the BQ**
   over hours — each re-enable restarted the charge cycle so it never established. Removed (v48).
2. **`bsp_display_start()` did `set_dir(0xffff, OUTPUT)`** — seizing all 16 PCA9535 pins including
   `EXP_GPO10` (`/CE`, active-low) at the POR-high latch = **charge disabled every boot**. The factory
   re-drives `BAT_CHARGE_EN=0` right after its own `0xffff`; we'd omitted that line. Mirrored it (v49).
3. `charge_task` rewritten to the factory model (hysteresis, **no watchdog**) + re-assert `/CE` each
   loop + externally switchable modes.
Fixing all three was correct and necessary — but did **not** produce sustained charging. (Commits
`2c16c1d`, `76828bd`.)

## Behavioral facts established (via the live pin/charge tools)
- `/CE` (`EXP_GPO10`) confirmed **LOW at the physical pin** = charge enabled. `VSYS_PG=0`. USB rail
  healthy 4.6–4.8 V. Every `ha_battery` pin verified against the factory BSP.
- A **~2 s `/CE`-high reset pulse flips STAT to 0** ("charging") **on demand**, then it **re-latches to
  STAT=1 within ~1–2 min**. **VBAT never rises**, latched or not, screen on or off, min load or full.
- No in-firmware current telemetry exists (no INA / no fuel-gauge / charger not on I²C) — VBAT dV/dt is
  the only signal; measuring real charge current needs an **external meter**.

## Live exploration tools built into the beachhead (committed, reusable)
- `cmd/gpio N [0|1]` / `cmd/exp N [0|1]` — read/drive any P4 or PCA9535 pin; readback → `d1001-beachhead/pin`.
- `cmd/charge auto|hold|on|off|reset[ ms]` → `d1001-beachhead/charge`; `cmd/screen off|on|toggle`.
- `ha_battery`: `ha_battery_charge_mode()` (AUTO/HOLD/ON/OFF) + `ha_battery_charge_reset_pulse(ms)`.

## Reproduce the factory-code harness (recipe)
1. `git clone https://github.com/Seeed-Studio/reTerminal-D1001` → `examples/factory_firmware`.
2. In `main/main.cpp` `app_main`: after `bsp_spiffs_mount()`, add a task logging
   `bsp_battery_voltage_read()/bsp_usb_voltage_read()/bsp_battery_charge_status_read()/gpio 15/gpio 4`,
   then `return;` (drops codec/display/Brookesia — the bootloop cause; charge path is all in
   `bsp_power_init()` which runs first).
3. `idf.py set-target esp32p4 && idf.py build && idf.py -p /dev/ttyACM0 flash`. Console = USB-serial-JTAG;
   read with pyserial. Every USB re-enumeration resets `/dev/ttyACM0` perms → `sudo chmod 666` (I'm not
   in `dialout`). A bootlooping image thrashes perms — flash in a retry loop (esptool's reset→download
   mode stops the loop).

## If resumed (NOT now)
1. **Latch vs hardware:** add a one-shot `/CE` reset to the harness (or un-latch via the beachhead's
   `cmd/charge reset`, then reflash the harness — BQ state persists across the reflash) so the factory
   code runs on a *guaranteed-unlatched* BQ. VBAT rises → it was the latch (give `ha_battery` a proper
   one-shot reset); still flat → **hardware**, escalate.
2. **Measure:** inline USB-C ammeter, and/or a meter in series with the battery lead — the only way to
   see actual charge current (no in-firmware telemetry).
3. Check cell health / BQ / board directly (Hugh, EE).

## Engineering wisdom (the durable takeaways)
- **Docs first** ([[feedback-docs-first]]): the schematic + vendor BSP existed the whole time; inferring
  hardware from I²C scans + an LLM overview produced several wrong facts (ES7210 audio ADC misread as an
  "INA" twice, "absent" fuel gauge, an invented P4-GPIO10 bug) before the schematic settled them.
- **Firmware is the problem until the reference proves otherwise** ([[feedback-firmware-is-the-problem]]):
  the default cause of a misbehaving-on-working-hardware bug is *our* code; you may not blame hardware
  until the reference (factory) code is run **verbatim** and reproduces the fault. Running the factory
  `bsp_power_init` byte-for-byte is exactly what finally licensed the hardware conclusion — and along the
  way it forced out three real firmware bugs we'd otherwise have blamed on the silicon.
- **Build an instrument, not a conclusion:** the live pin/charge MQTT tools turned a reflash-per-guess
  loop into live exploration.
