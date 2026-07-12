# ZeroFlow / Elecrow WiFi Fan Controller (ESP32-S2) — design, capabilities, intake

**Device:** Elecrow **PIA10400P** = ZeroFlow / Arthofer Engineering *WiFi Fan Controller* (open ESPHome design).
**Qty on hand:** 2 (bench). Goal: research + bench-test, then intake onto the WiFi fleet.
**Status (2026-07-12):** **BOTH boards FLASHED with our config.** Board 1 bench-verified LIVE (curve/telemetry/
tach/PWM all green — see below); board 2 flashed + verified booting/curve/telemetry as `fancontroller-2` (distinct
identity via package-include; 12V/fans not attached so tach/PWM inherit board 1's proof). Deferred: system
registration (storage ingest) + CT200 `FLOOR` tuning. Deployment target: 2 controllers × 4× Thermaltake CT200 (4-pin PWM) each,
push-pull at opposite gable ends — house attic (test, WiFi reaches) → garage attic (permanent, WiFi does NOT
reach, so the curve runs 100% on-device).

## Our config + bench verification (board 1, 2026-07-12)
- **Config:** [`provisioning/fancontroller/fancontroller.yaml`](../../provisioning/fancontroller/fancontroller.yaml)
  — self-contained (no `github://` include; air-gap-clean), Levoit networking skeleton (`api: reboot_timeout: 0s`,
  un-hidden `autohome_airgap` + household fallback, `safe_mode`, MQTT→**.210 bench** / flip to VIP **.200** for prod),
  fresh per-device OTA/AP keys. First flash = USB-C cable; thereafter OTA.
- **On-device fan curve** (network-independent): linear **80°F→100°F** (26.7→37.8 °C), floored at **30%** min-spin,
  hysteresis (on 80°F / release 77°F), **fail-safe → 100% on HDC1080 NaN**. All 4 fans track together.
- **Dropped the RGB LED strip** (clears the stock `esp32_rmt_led_strip` "no free tx channels" boot error) + **pinned
  its data line GPIO01 LOW** so nothing lights (Hugh: no LEDs). Any hardwired power LED is not SW-killable (tape).
- **Dead-fan alarm:** `fan_fault` (problem) trips if the curve commands motion but a tach reads 0 RPM 20s+.
- **Bench-verified LIVE:** boot ✅ · HDC1080 temp+hum ✅ · curve math incl. NaN→full ✅ · all-4 drive ✅ · tach RPM ✅ ·
  **4-pin PWM speed control ✅** (visually confirmed) · MQTT telemetry→.210 ✅ · closed loop (airflow cooled board →
  duty walked down) ✅ · LEDs off ✅. Note: bench WiFi flapped (marginal signal) — control ran straight through it.
- **DEFERRED follow-ups:** (1) register on the system (`devices.yaml`/`control.yaml` + trace whether ESPHome-MQTT
  telemetry auto-ingests to storage or needs a Levoit-style bridge); (2) characterize CT200 min-spin `FLOOR` +
  confirm its 500–900 RPM tach range (`multiply: 0.5` = 2 ppr carries over) — both via OTA when the CT200s land.
  - **Board 2 config:** [`fancontroller-2.yaml`](../../provisioning/fancontroller/fancontroller-2.yaml) — thin: `packages: !include
  fancontroller.yaml` + overrides `device_name`/`friendly`/`wifi_ap_ssid` to `fancontroller-2` (main-file substitutions win
  over the package's), so curve/hardware/networking stay single-source in `fancontroller.yaml`. Shares `secrets.yaml` (same WiFi + OTA key).
- **Bench tooling note:** the sandbox blocks foreground `sleep`, so paced MQTT command tests fail; verify PWM either
  by watching the fan or with a throwaway on-device duty-sweep firmware (logs duty→RPM over serial).

This is a **WiFi ESP32-S2 ESPHome** device → it belongs to the **WiFi intake fleet** (like the Levoit V201S and the
E1001 panel), **not** the signed native-C edge fleet (the C6 gas nodes). See [docs/DEVICE-INTAKE.md](../DEVICE-INTAKE.md)
and [docs/device-onboarding.md](../device-onboarding.md).

## Vendor / provenance
- Product page: https://www.elecrow.com/wifi-fancontroller1.html  (model **PIA10400P**, "Made for ESPHome", CE)
- Open design + firmware: https://github.com/zeroflow/esphome-fancontroller
- Docs / FAQ / pinout: https://fancontroller.arthofer.dev/
- Hardware package YAML: `hardware-rev-3.3.yaml` → `!include hardware-rev-3.1.yaml` (the real definitions live in rev-3.1)

## As-shipped firmware (read off the bench unit 2026-07-11, USB `/dev/ttyACM0`, USB id `303a:0002`)
- **ESPHome 2026.2.2**, compiled 2026-02-26; project **`zeroflow.fancontroller-rev3-3` v3.3**
- ESP-IDF 5.5.2 bootloader; **ESP32-S2 rev v1.0, 4 MB flash**
- Partition table: **dual OTA** (`app0`/`app1`, 0x1c0000 each) + `nvs`, `otadata`, `eeprom`, `spiffs`
  → **OTA-updatable after the first cable flash** (matches the rest of the WiFi fleet lifecycle)
- Transport: **native ESPHome API only, NO MQTT** in the stock image. On first boot it sits in **WiFi AP-fallback**
  (`AP SSID: fancontroller-r3-3-70e3a6`), unprovisioned.
- Boot log shows one harmless defect: `esp32_rmt_led_strip … no free tx channels` — the S2 has fewer RMT channels
  than the stock config wants LED strips (per-fan + board + NeoPixel). Cosmetic; trimmable in our own config.

## Capabilities (hardware + firmware)
| Function | Detail |
|---|---|
| Fan speed × 4 | ESPHome `ledc` PWM outputs on **GPIO12,13,14,15 @ 25 kHz** → drives the **4-pin fan PWM *signal* wire** |
| Fan tach × 4 | `pulse_counter` on **GPIO16,17,18,21**, RPM, `multiply: 0.5` (2 pulses/rev) |
| Temp + humidity | **HDC1080** on I²C **@ 0x40**, 60 s update — device-classed temperature + humidity |
| Buttons × 3 | `USR1/2/3` on GPIO38/37/36 (interrupt, any-edge) |
| LEDs | RGB status per-fan + board + NeoPixel (RMT-channel-limited on S2, see above) |
| Expansion | Qwiic / I²C header, USB-C (**flashing only**) |
| Power | **12 V DC barrel jack (5.5×2.1 mm), always required for fans**; 2.5 A max *total* (all fans + board) |

## ⚠️ Key limitation: 3-pin (DC) fans are NOT speed-controllable on this board
Confirmed from both the firmware (`hardware-rev-3.1.yaml`) and the vendor FAQ ("Any standard 4-pin PWM fan… 12 V only").

- The board is a **PWM-*signal* controller, not a power controller.** The four `ledc` outputs drive the fan's **blue
  PWM signal wire** at 25 kHz. **The fan +12 V rail is hardwired always-on — there is NO per-channel MOSFET / enable /
  power-switching stage** (none in the schematic-level design, none in firmware).
- Therefore a **3-pin fan** (GND / +12 V / Tach, no PWM wire) plugged in:
  - runs at **100 %, full speed, uncontrollable** (the PWM signal has no wire to land on);
  - **cannot be switched off from the board at all** (no PWM wire to command, and no power cutoff);
  - **RPM/tach still reads fine** (3rd pin), and **HDC1080 temp/humidity is unaffected**.

### How "off" works on this board (no hard power cut)
Confirmed 2026-07-11 against 3 independent sources (firmware `hardware-rev-3.1.yaml`, docs FAQ, hardware repo):
**there is NO MOSFET/relay/enable on the 12 V fan rail — global or per-channel.** The 12 V is always-on.
- **4-pin PWM fans:** "off" = **PWM duty 0 %** on the signal wire. Whether the fan *fully stops* is **fan-dependent** —
  quality PC fans (Noctua, Arctic) stop at 0 %; some server/industrial fans idle at a floor. There is **no hard
  power-cut backstop** if a fan ignores 0 %.
- **3-pin fans:** **no off at all** — no signal wire, no power cut. They run wide-open until upstream power is removed.
- **The only true "all-fan power off" is upstream of the board:** cut the **12 V barrel-jack feed** (e.g., put the 12 V
  supply on a Tasmota/smart plug or an inline relay). The board itself has no master kill.
- **Voltage regulation is NOT possible from firmware** and not possible with the stock board. The only route is an
  **external hardware mod**: a low-side N-MOSFET (+ flyback diode) per fan in the ground leg, gated off a GPIO12–15
  output, then PWM the power rail. Caveats: chopping fan ground **garbles the tach**, 3-pin motors often **buzz** under
  power-PWM (want true linear/buck DC for clean results), and it's a soldering mod on an otherwise-stock board.
- **Recommendation:** use **4-pin PWM fans** (what the board is built for) → smooth 0–100 % + clean tach + no mods.
  200 mm 4-pin PWM fans exist (Noctua NF-A20 PWM, Arctic, etc.). Treat the 3-pin MOSFET mod as a deliberate hardware
  project only if there's a specific reason to drive the existing 3-pin fans.

## Intake / firmware decision (2026-07-11)
**Roll our own ESPHome config on top of the ZeroFlow package — stay ESPHome, do NOT port to native-C.**
- Platform = **ESPHome** — purpose-built board, working vendor config, matches the WiFi intake fleet. Native-C buys
  nothing (no signing requirement on a WiFi actuator) and would discard a working fan/RPM/HDC1080 config.
- Config = **ours, based on ZeroFlow's** — because we must change transport (stock native-API → **MQTT to the air-gap
  broker**) and bake in house conventions: `api: reboot_timeout: 0s` (the documented 15-min-reboot gotcha, see memory
  `esphome-api-reboot-timeout`), `autohome_airgap` WiFi + **un-hide SSID during intake** (memory `intake-unhide-ssid`),
  device/area naming + area stamping, broker = **.210 for dev-bench** (NOT the VIP .200). Trim unused LED strips to
  clear the RMT error while we're in there.
- First flash is over **USB-C cable**; everything after is **OTA**.

## Bench procedure notes
- Read stock logs **without resetting** the board: `tools/bench_serial_read.py /dev/ttyACM0 115200 <secs>`
  (opens the port with DTR/RTS de-asserted so the ESP32-S2 is not reset — plain `cat`/`screen` toggle reset lines).
- USB-C is data/flash only; **12 V barrel jack must be connected** for the fans to spin.
- `tools/esphome` is the repo's ESPHome CLI wrapper (use for compile/flash/logs once we stage our config).
