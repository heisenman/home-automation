# D1001 display fix — MIPI-DSI / PSRAM framebuffer starvation (flicker + wedge)

**Status:** plan / decompose-before-dev. Root cause CONFIRMED 2026-07-05 via USB console capture. Implement
next round. Folds in the parked "LVGL internal-vs-PSRAM perf refactor" (they are the same work).
**Board:** `d1001-psram-dsi-flicker` (root cause recorded) + `d1001-ota-wedge-investigate` (same root) →
merge into one effort.

## Root cause (confirmed, not a theory — refined 2026-07-06 by reading the IDF 5.4 DSI driver)
`taskLVGL` stalls in LVGL's `wait_for_flushing` (`lvgl/src/core/lv_refr.c:1458`, addr2line of MEPC
`0x4003f7fe` on the v74 elf) → IDLE0 starves → Task Watchdog fires every 5 s → panel hung (display frozen,
MQTT offline). Captured on the console at ~90 s uptime, mid replica boot-pull.

**Mechanism (now traced through the driver, not inferred):** our display path takes the **synchronous
CPU-copy flush branch** in `esp_lcd_panel_dpi.c:505-525` (`dpi_panel_draw_bitmap`). With `num_fbs=1`,
`use_dma2d=false`, and the LVGL draw buffer allocated **separately in PSRAM**, each LVGL flush does, *on the
taskLVGL stack*: a per-line `memcpy` (PSRAM draw buf → PSRAM framebuffer) + `esp_cache_msync` writeback to
PSRAM, then a **synchronous** `on_color_trans_done`. So the flush is a blocking CPU+bus operation, not an
async wait. During a replica pull, `ha_replica`'s HTTP→SD DMA (`http_get_to_file`, ~L82-85, a tight unyielding
loop) saturates the PSRAM/AXI bus, so that memcpy+msync takes **seconds** — taskLVGL (high prio) never yields
→ IDLE0 WDT. The MIPI-DSI DPI DMA also underruns from the same saturation (`esp_lcd_panel_dpi.c:94`
`ESP_DRAM_LOGE(... "can't fetch data from external memory fast enough, underrun happens")`) → the cosmetic
blue/flicker. **Flicker and wedge are the same bus-bandwidth event: flicker = the DSI underrun, wedge = the
synchronous copy stalling past the WDT window.** Intermittent (worst during the 48 MB hot.db pull). v73 does
it too — NOT the v74 module cuts (they don't touch LVGL).

**DOCS-FIRST CORRECTION (this doc's original Part B was wrong):** IDF 5.4's `esp_lcd_dpi_panel_config_t`
(`esp_lcd_mipi_dsi.h`) has **no `bounce_buffer_size_px` field** — that lever exists only for the RGB LCD
peripheral (`esp_lcd_rgb_panel_config_t`), not MIPI-DSI DPI. The DSI scanout is inherently PSRAM-fed at this
resolution (2 MB fb can't fit internal SRAM). Espressif's own underrun comment points at *"optimize the memory
bandwidth (with AXI-ICM)"*, i.e. reduce competing traffic / raise the DSI's bus QoS — there is no bounce
buffer to make scanout immune. So the real levers are: (1) **reduce the competing DMA** (throttle the pull),
(2) **shrink LVGL's own flush cost & move its render off PSRAM** (internal-SRAM partial draw buffer), and only
if needed (3) **make the copy async** (`use_dma2d=true`, so taskLVGL yields instead of spinning).

## Current config = the problem (`bsp_display.c` ~L173-208)
```c
dpi.num_fbs = 1;                        // single PSRAM framebuffer (2 MB) — bring-up default
.buffer_size = LCD_H_RES * LCD_V_RES,   // FULL-screen LVGL draw buffer (2 MB), PARTIAL render mode
.double_buffer = false,
.flags = { .buff_spiram = true, ... }   // LVGL draw buffer ALSO in PSRAM
// dpi.flags.use_dma2d not set => flush copies on the CPU, synchronously (the stall)
```
Everything on the display path is in PSRAM, single-buffered — the untouched bring-up config. With
`avoid_tearing=false` + no direct/full-refresh flag, `esp_lvgl_port` renders in **PARTIAL** mode into a
full-screen PSRAM draw buffer, then flushes via the synchronous CPU-copy path above. This is the parked
"LVGL internal-vs-PSRAM refactor": move LVGL's hot render buffer off PSRAM.

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

### Part B — robust: move LVGL's render off PSRAM + shrink the synchronous flush (the refactor)
There is **no DSI bounce buffer** in IDF 5.4 (see correction above), so we cannot make the scanout immune.
What we *can* do is (a) stop LVGL from rendering into PSRAM and (b) make each flush copy tiny, so the
synchronous memcpy+msync can't stall for seconds even under bus pressure. Levers (in `bsp_display.c`
`lvgl_init()`):
1. **LVGL draw buffer → internal SRAM + partial** — set `.buff_spiram = false` AND shrink `buffer_size` to a
   partial buffer (~1/10 screen: `LCD_H_RES * (LCD_V_RES/10)` = 800×128 = 200 KB). PARTIAL render mode allows
   this (LVGL recommends ≥1/10 screen). Now LVGL renders into fast internal SRAM (off the contended bus), and
   the flush's `memcpy` reads from internal SRAM; only the small dirty-region writeback crosses to the PSRAM
   framebuffer. Cuts LVGL's PSRAM traffic hugely and shrinks the synchronous stall. **This is the primary
   Part B fix.**
2. **Escalation only if WDT survives — `dpi.flags.use_dma2d = true`** — moves the draw-buffer→framebuffer
   copy onto the DMA2D engine (async): `dpi_panel_draw_bitmap` returns immediately, `on_color_trans_done`
   fires from the DMA2D-done ISR, and taskLVGL **blocks-with-yield** on flush-ready instead of spinning the
   CPU. This directly removes the IDLE0-starvation path (worst case a flush just takes longer; no WDT). Valid
   with `num_fbs=1` on P4 (verified: `esp_lcd_panel_dpi.c:242` sets up `fbcpy_handle` whenever `use_dma2d`,
   no `num_fbs>=2` requirement). Adds a `draw_sem` "previous draw not finished" contract — slightly more risk,
   so hold it in reserve.
3. **Not doing: `num_fbs=2` / `avoid_tearing=true`.** Double-framebuffer pins a second 2 MB PSRAM fb and
   flushes on vsync — it *increases* PSRAM scanout pressure and doesn't address the wedge. Skip unless a
   tearing problem appears after (1)+(2).
4. **Internal-SRAM budget check** — a 1/10 internal draw buffer is a ~200 KB contiguous alloc. Confirm free
   contiguous internal DRAM with `idf.py size` + the boot heap log; if the alloc fails/fragments, drop to
   1/16 (800×80 = 128 KB) or 1/20 (800×64 = 100 KB). Single buffer first (no `double_buffer`) to stay within
   budget; add a second small internal buffer only if flush/render overlap needs it.

Start with (1) — off-PSRAM partial draw buffer — then add (2) `use_dma2d` only if a forced full-pull still
trips the WDT.

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
2. **Part B internal-SRAM partial draw buffer** — the real fix; measure WDT gone + underruns reduced under a
   full pull.
3. **Part B `use_dma2d`** — only if (1)+(2) still trip the WDT under a forced full pull.
4. Close `d1001-psram-dsi-flicker` + `d1001-ota-wedge-investigate` when a full-pull soak shows zero WDT and no
   (or rare, cosmetic-only) underruns. The parked "LVGL internal-vs-PSRAM refactor" is subsumed here.
