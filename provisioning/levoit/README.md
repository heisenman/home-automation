# Levoit Vital 200S (LAP-V201S) → local ESPHome on the HA bus

Recipe + reference for converting a cloud-locked Levoit Vital 200S to fully-local ESPHome control, proven
live on **2026-06-27** (device `levoit-office`). No VeSync, no internet at runtime. The same flow applies to
the other Vital/Core Levoits (different `model:` + board).

## The device
- **Model:** Levoit Vital 200S (LAP-V201S-AUSR). MCU speaks the Vital **TLV** UART protocol.
- **WiFi module:** onboard **ESP32-C3-SOLO-1** (single-core C3, 4 MB embedded flash) — *reflashed in place*,
  no added microcontroller, no level shifter.
- **Component:** [`github://tuct/levoit`](https://github.com/tuct/levoit) `model: VITAL200S` (the fork with
  tested Vital support; acvigue is Core-only).

## Key facts (the non-obvious bits)
- **MCU UART pins ≠ flash pads.** Flashing is over UART0 (`TXD0/RXD0/IO0/3V3/GND` debug pads). The firmware
  talks to the purifier MCU on **`tx=GPIO19`, `rx=GPIO18`** @115200 (the C3's USB-JTAG pins, repurposed —
  ESPHome warns about this; expected, we don't use USB serial).
- **MQTT broker = `192.168.0.210`, NOT the VIP `.200`** — the VIP is unreachable from `CTWap_24g` wifi
  (known `vip-unreachable-from-wifi` issue); wifi devices target the dictator's real IP.
- **Beachhead-first strategy.** First flash a minimal WiFi+OTA+fallback-AP+MQTT image and prove it connects;
  THEN add the levoit component over OTA. Once sealed in the unit you can't easily re-wire, so never gamble
  the first flash on an unproven UART config. `captive_portal` + fallback AP = wireless recovery, always.

## Flash / build / OTA workflow
```bash
# 0. tooling (one-time): python3 -m venv ~/.flashtools && ~/.flashtools/bin/pip install esptool ; docker present
# 1. BACK UP the OEM firmware first (4 MB):
~/.flashtools/bin/esptool --port /dev/ttyUSB0 read-flash 0x0 ALL levoit-oem-backup.bin
# 2. build (Docker ESPHome; needs internet once):
docker run --rm -v "$PWD":/config ghcr.io/esphome/esphome compile levoit-vital200s-c3.yaml
# 3. first flash over serial (IO0 grounded at power-on; chip = ESP32-C3):
~/.flashtools/bin/esptool --port /dev/ttyUSB0 --baud 460800 write-flash --erase-all 0x0 \
    .esphome/build/<name>/.pioenvs/<name>/firmware.factory.bin
# 4. thereafter OTA only (no reopening):
docker run --rm -v "$PWD":/config ghcr.io/esphome/esphome upload levoit-vital200s-c3.yaml --device <ip-or-name.local>
#    NB: `upload` does NOT recompile — run `compile` first (or use `run`) after any config change.
```
Config = `levoit-vital200s-c3.yaml` (secrets in `secrets.yaml`, see `secrets.example.yaml`). OEM backup is
kept **off-git** (restore image; may carry VeSync creds). Recovery: fallback AP `levoit-office-fallback`.

## MQTT topic map (for the canonical bridge — INTEGRATION TODO)
Device name `levoit-office` (IP `192.168.0.252`, MAC `dc:1e:d5:3d:34:d0`). ESPHome publishes its **own**
layout — a bridge must map it into our canonical `home/<area>/<device_id>/state` (mirror
`server/ingest/tasmota_bridge.py`). Availability: `levoit-office/status` = `online|offline`.

**Read (state) — `<topic>` → suggested canonical metric:**
| ESPHome topic | metric |
|---|---|
| `levoit-office/sensor/pm_2_5/state` | `pm25_ugm3` |
| `levoit-office/sensor/aqi/state` | `aqi` |
| `levoit-office/sensor/current_cadr/state` | `cadr` |
| `levoit-office/sensor/filter__/state` | `filter_life_pct` |
| `levoit-office/fan/fan/state` (ON/OFF) | `fan_on` (1/0) |
| `levoit-office/fan/fan/speed_level/state` (1–4) | `fan_speed` |
| `levoit-office/binary_sensor/filter_low/state` | `filter_low` (1/0) |
| `.../sensor/{mcu_version,esp_version,error}/state`, `.../select/*`, `.../number/*`, `.../switch/*` | metadata / advanced controls |

**Write (command) — publish to the matching `…/command` topic:**
| action | topic | payload |
|---|---|---|
| fan on/off | `levoit-office/fan/fan/command` | `ON` / `OFF` |
| fan speed | `levoit-office/fan/fan/speed_level/command` | `1`–`4` |
| display / child-lock / presets | `levoit-office/switch/<name>/command` | `ON` / `OFF` |
| auto / sleep / daytime mode | `levoit-office/select/<name>/command` | a listed option |

## Integration TODO (handed to dev — board `levoit-integration`)
1. **Publish into the system:** ESPHome→canonical bridge (mirror `tasmota_bridge.py`) → `home/office/levoit_office/state`
   → writer → `hot.db`. Add `pm25_ugm3`/`aqi`/`fan_speed`/etc. units to `writer._UNITS`. Registry entry.
2. **Control on the web app:** a purifier card in the PWA — show PM2.5/AQI/filter; control fan on/off + speed
   + Auto/Sleep mode (commands flow PWA → `…/command`).
3. **Automations:** PM2.5 threshold → fan, the way the dehumidifier runs off humidity (scene/threshold-driven).
