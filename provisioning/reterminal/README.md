# Seeed reTerminal panels (ESP32) → HA screen interfaces

Intake reference for the Seeed reTerminal display family we're bringing onto the HA system as room
control/status panels. Home of the firmware-backup procedure (proven on the **D1001**, 2026-07-01),
the per-device facts, and a pointer to the panel architecture project.

The overall "screen interface for the HA system" architecture is **[ADR-0019](../../docs/adr/ADR-0019-screen-interface-architecture.md)**
(Proposed, 2026-07-01) — this dir is the device/provisioning half; ADR-0019 is the design half (panel =
"PWA-in-firmware" reusing the live BFF + trait model; stable host + swappable manifest-driven app; panels
double as local data-recovery nodes). It builds on `docs/adr/ADR-0013-presentation-architecture.md`, which
already frames MCU panels as a first-class API-first client (BFF view-model + MQTT).

## The devices

| | **reTerminal D1001** (here now) | **reTerminal E1001** (incoming) |
|---|---|---|
| Compute | ESP32-**P4** (dual RISC-V @400 MHz, 32 MB PSRAM, 32 MB flash) | ESP32-**S3** (native WiFi 4 / BLE 5) |
| Radio | **no onboard radio** — ESP32-**C6** coprocessor (WiFi-6/BLE) via `esp-hosted` | native (S3) |
| Display | 8" **color LCD, capacitive touch**, fast | 7.5" **mono ePaper**, 4-level grey, seconds-slow refresh |
| Firmware | **ESP-IDF only** (Seeed-supported); LVGL for UI | ESPHome / Arduino / ESP-IDF (Seeed supports **ESPHome**) |
| Extras | MIPI-CSI camera (SC2356) — **we disable it**, mic/speaker, 2500 mAh battery | onboard **T/H sensor** + buzzer, 2000 mAh battery (~3-month) |
| Enclosure button | **one green button only**: long-press >3 s = power on; short-press = screen off/on | buttons + LEDs |
| Factory firmware | https://github.com/Seeed-Studio/reTerminal-D1001 (ESP-IDF demo) | SenseCraft / ESPHome |

**Battery is a per-panel UPS only** — powers just that panel (ride-through mains flickers, graceful
shutdown, local "offline" state). It cannot back up the servers/broker/router; true system ride-through
is a separate core-box-UPS decision.

**microSD is REQUIRED for the data-recovery role** (ADR-0019 §4). Onboard 32 MB flash is firmware/app only;
PSRAM is volatile. A background data-agent (P4 second core) subscribes to the full `home/+/+/state` stream
and persists a batched rolling archive to SD → the panel serves charts from local cache (instant/offline)
and acts as a distributed recovery copy below the warm standby. Spec: **high-endurance (dashcam-rated)
microSD, ~32 GB** — years of retention + wear headroom for 24/7 batched writes; removable = a recovery win
(readable even from a dead panel). Continuous-capture recovery is an **always-on (D1001)** role — the
deep-sleep **E1001** can only snapshot, not capture gaplessly.

**Camera: disabled at firmware level** on the D1001 (never init the MIPI-CSI/SC2356 driver, rail off) —
privacy on a wall panel. Not a software toggle.

## Firmware backup (do this FIRST, before ever overwriting)

Same discipline as the Levoit OEM backup: image the factory flash before touching it. The D1001's demo
contains a working `esp-hosted` C6 setup we'll want as reference, plus possible Seeed calibration/NVS.
**Keep the backup off-git** (may carry vendor creds/cal).

> **Our unit's outcome (2026-07-01):** imaged to `~/reterminal-d1001-factory-backup.bin` (32 MB) with the
> resilient reader below — **one 64 KB sector at `0x600000` was unreadable** ("Packet content transfer
> stopped", marginal/flaky on retry, *not* a cable issue — every other region reads at full speed; Flash
> Encryption + Secure Boot both **Disabled**). That sector is zero-filled and logged to
> `~/d1001-backup/gaps.txt`. 99.8 % captured, incl. bootloader/partition table/NVS. Noted as a device-health
> flag: watch for touch/boot glitches; a bad flash sector on a new unit is grounds for exchange if functional
> problems appear.

The chunked loop below is the first-try for a **healthy** device. If a chunk hits an **unreadable sector**
(as ours did), use **`resilient-flash-backup.sh`** (this dir) — it subdivides a failing block to 64 KB and
zero-fills only the dead sectors, capturing everything else:
`PORT=/dev/ttyACM0 SIZE=$((32*1024*1024)) ./resilient-flash-backup.sh ~/reterminal-d1001-factory-backup.bin`

### The procedure that WORKS (D1001, 32 MB over USB-Serial/JTAG)

No buttons needed — esptool enters download mode automatically over USB-C (`--before default-reset`).
The naive `read-flash 0x0 ALL` **crashes mid-transfer** on this chip; the reliable recipe is
**stub loader + small chunks + auto-reset per chunk + retry**:

```bash
mkdir -p ~/d1001-backup && cd ~/d1001-backup && rm -f part_0x*.bin
for off in 0x0000000 0x0200000 0x0400000 0x0600000 0x0800000 0x0A00000 0x0C00000 0x0E00000 \
           0x1000000 0x1200000 0x1400000 0x1600000 0x1800000 0x1A00000 0x1C00000 0x1E00000; do
  for try in 1 2 3; do
    echo ">>> $off (attempt $try)"
    ~/.flashtools/bin/esptool --port /dev/ttyACM0 --before default-reset --after no-reset \
      read-flash $off 0x200000 part_$off.bin && break
    [ "$try" = 3 ] && { echo ">>> GAVE UP at $off"; exit 1; }
    sleep 2
  done
done
cat part_0x*.bin > ~/reterminal-d1001-factory-backup.bin   # fixed-width hex globs sort in order
ls -l ~/reterminal-d1001-factory-backup.bin                # expect 33554432 bytes (32 MB)
```

### Why these flags (the reusable lessons — apply to any large/flaky USB-JTAG flash)

- **Use the stub, NOT `--no-stub`.** Counterintuitive, but the ROM loader is far slower and *less*
  stable for long reads — with `--no-stub` the D1001 died at ~8 KB; with the stub it reached ~5–6 MB.
  Keep the stub (the default) and chunk under its reach.
- **Chunk small (2 MB) with per-chunk auto-reset (`--before default-reset --after no-reset`).** Each
  chunk is a fresh, short, recoverable transfer; a failure costs 2 MB, not the whole image. `no-reset`
  after leaves it in the bootloader so the next `default-reset` re-enters download mode cleanly.
- **No boot buttons required** on native-USB Espressif parts (P4/S3/C3) — esptool drives download mode
  over USB itself. Pre-assembled enclosures (like the reTerminal) often expose no BOOT/RESET at all.
- **`chip-id` / `flash-id` first** — read-only, confirms the chip + that auto-download-mode works before
  any long operation. (D1001 reports `ESP32-P4`, no chip-id → shows MAC instead; that's normal.)
- **>16 MB caveat:** esptool 5.3.0 warns "flash sizes larger than 16 MB are not fully supported." If a
  32 MB device reads fine up to `0x0E00000` then fails on chunks from `0x1000000`, it's the 16 MB
  addressing limit (not power) — bump esptool to a newer build or enable 4-byte flash addressing.
- **Rule out brownout** for mid-read crashes: direct USB port (not a bus-powered hub), battery charged.
- **Keep the image off-git.** Vendor firmware may carry creds/calibration (cf. the Levoit OEM backup).

Tooling: `esptool` in the `~/.flashtools/` venv (PEP668 blocks system pip). Port is `/dev/ttyACM0`
(native USB CDC). Desktop `visko` isn't in `dialout` → `sudo chmod 666 /dev/ttyACM0` after replug
(or add to dialout once). See `[[reference-esphome-device-intake]]`-style intake notes in memory.

## Getting on the HA bus (the hard part — see ADR-0019)

Unlike the Levoit/S31, ESPHome's turnkey path does **not** apply to the D1001: the P4 has no native
WiFi, and ESPHome doesn't support the `esp-hosted` C6-as-coprocessor arrangement. The D1001 route is a
custom **ESP-IDF + LVGL + esp-hosted + MQTT** build (broker `192.168.0.210`, `home/<area>/<device_id>`
canonical topics + command topics per ADR-0014 + `home/_alerts`). The E1001, by contrast, has native S3
WiFi and Seeed-supported **ESPHome**, so it can reuse the ESPHome intake flow. The panel architecture
(a shared declarative "screen spec" rendered per-device) is ADR-0019 — that's what makes one design
serve both a P4/LVGL touch panel and an S3/ePaper display.
