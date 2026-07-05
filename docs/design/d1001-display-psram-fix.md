# D1001 display fix — MIPI-DSI / PSRAM framebuffer starvation (flicker + wedge)

**Status:** plan / decompose-before-dev. Root cause CONFIRMED 2026-07-05 via USB console capture. Implement
next round. Folds in the parked "LVGL internal-vs-PSRAM perf refactor" (they are the same work).
**Board:** `d1001-psram-dsi-flicker` (root cause recorded) + `d1001-ota-wedge-investigate` (same root) →
merge into one effort.

## Root cause (confirmed, not a theory)
`taskLVGL` hangs in LVGL's `wait_for_flushing` (`lvgl/src/core/lv_refr.c:1458`, addr2line of MEPC
`0x4003f7fe` on the v74 elf) — it calls the display flush, then spins waiting for the driver to signal
flush-done, **which never comes** → IDLE0 starves → Task Watchdog fires every 5 s → panel hung (display
frozen, MQTT offline). Captured on the console at ~90 s uptime, mid replica boot-pull.

**Mechanism:** the MIPI-DSI DMA continuously scans the **2 MB framebuffer out of PSRAM** (60 Hz RGB565 ≈
120 MB/s just for scanout). The `ha_replica` pull streams HTTP→SD in a **tight, unyielding loop**
(`ha_replica.c` `http_get_to_file`, ~L82-85), whose SD + HTTP DMA saturates the same memory bus. The DSI's
PSRAM fetch starves → `E lcd.dsi.dpi: ... underrun happens` → when a flush stalls past the WDT window,
`wait_for_flushing` never returns. **Flicker = mild underrun; wedge = severe (flush hang). Same bug.**
Intermittent (survives some pulls; hot.db 48 MB pull is the worst window). v73 does it too — NOT caused by
the v74 module cuts (they don't touch LVGL).

## Current config = the problem (`bsp_display.c` ~L173-208)
```c
dpi.num_fbs = 1;                 // single PSRAM framebuffer (2 MB) — "plenty for a first bring-up"
.buffer_size = LCD_H_RES * LCD_V_RES,   // full-screen LVGL draw buffer
.double_buffer = false,
.flags = { .buff_spiram = true, ... }   // LVGL draw buffer ALSO in PSRAM
```
Everything on the display path is in PSRAM, single-buffered, **no bounce buffer** — the untouched bring-up
config. `lv_mem_psram.c`'s own header already flags the follow-up: *"inventory what LVGL allocates vs what
else lives in PSRAM and rebalance hot allocations back [to internal]"* — that IS the parked refactor.

## The fix — two parts (interim + robust)

### Part A — interim: throttle the replica SD writes (quick, low-risk)
Give the DSI bus breathing room during a pull so a flush can always complete. In `http_get_to_file`'s
read/write loop, yield periodically (not per-chunk — too slow for 93 MB). Tune to ~every 32-64 KB written:
```c
size_t since_yield = 0;
while ((r = esp_http_client_read(cl, buf, sizeof buf)) > 0) {
    if (fwrite(buf, 1, r, f) != (size_t)r) { ok = false; break; }
    total += r; since_yield += r;
    if (since_yield >= 32*1024) { vTaskDelay(pdMS_TO_TICKS(4)); since_yield = 0; }  // let the DSI catch up
}
```
Cost: ~93 MB / 32 KB × 4 ms ≈ +12 s on the cold boot pull (acceptable — pull already takes minutes).
Reduces the *trigger*; does NOT make the DSI immune. Verify: no `underrun`/WDT during a forced
`cmd/replica` full pull with the console attached. If underruns persist, tighten (24 KB / 6 ms).

### Part B — robust: move the display path off PSRAM-bandwidth dependence (the refactor)
Make the DSI scanout independent of real-time PSRAM latency. Levers (in `bsp_display.c` / `esp_lvgl_port`):
1. **DSI bounce buffer** — set `dpi.bounce_buffer_size_px` (a small internal-SRAM buffer the DSI scans out
   from, refilled from PSRAM in chunks). This is the direct anti-underrun fix. Size ~ 10-40 lines
   (`LCD_H_RES × N`), budget against internal SRAM (see below).
2. **LVGL draw buffer → internal SRAM** (`.buff_spiram = false`) — a **partial** buffer (e.g. 1/8–1/10
   screen), since a full 2 MB won't fit ~768 KB internal. LVGL renders partial regions to fast SRAM, then
   the port blits to the framebuffer.
3. **Double-buffer** (`num_fbs = 2`) so LVGL renders one frame while the DSI scans the other — removes
   tearing/contention on the single fb. Costs a second 2 MB PSRAM fb (PSRAM is plentiful, 29 MB free).
4. **Internal-SRAM budget check** — `idf.py size` shows ~393 KB DIRAM free today; a bounce buffer of
   800×40×2 = 64 KB + a partial draw buffer ~128 KB fits comfortably. Confirm with a build.

Start with (1) bounce buffer — it's the smallest change that directly kills the underrun — then add (2)/(3)
if underruns/flicker survive.

## Verification (per change, console attached)
- Force the worst case: `cmd/replica` full pull (incl hot.db) → watch the console for `lcd.dsi.dpi ...
  underrun` and `task_wdt` / `wait_for_flushing`. **Zero underruns under a full pull = fixed.**
- Heap/DIRAM before-after (`idf.py size`) — stay within internal-SRAM budget.
- Soak: leave it through several hourly pulls + a hot.db pull without a hang.

## Deployment discipline (today's lessons — non-negotiable next round)
- **Console attached for every flash** (`tools`/`console_cap.py`: follow the STABLE by-id symlink
  `/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_<MAC>-if00`; **do NOT assert DTR/RTS** — it's
  a USB-serial-JTAG, DTR/RTS drive reset/boot). The by-id fix is what finally captured the crash.
- **Prefer cable-flash over OTA** for iteration (`idf.py -p <by-id> flash`) — reliable, boots valid
  immediately, no pending-verify rollback. OTA here has an **intermittent first-attempt rollback** (~half
  the time; retry boots) that wastes cycles and once wedged the panel.
- Recovery for a hung panel (cabled): `esptool --port <by-id> --before default_reset --after hard_reset
  --chip esp32p4 flash_id` hard-resets it.

## Sequencing / ownership
1. **Part A throttle** — small, ships first as a stabilizer (ops).
2. **Part B bounce buffer** — the real fix; measure underruns gone under a full pull.
3. **Part B draw-buffer/double-buffer** — only if (1)+(2) leave residual flicker.
4. Close `d1001-psram-dsi-flicker` + `d1001-ota-wedge-investigate` when a full-pull soak shows zero
   underruns/WDT. The parked "LVGL internal-vs-PSRAM refactor" is subsumed here.
