# D1001 host beachhead (ADR-0019 Phase 1)

Minimal ESP-IDF app that **proves the connectivity stack** on the reTerminal D1001's ESP32-P4 before any
UI: boot → bring up the **ESP32-C6 over esp-hosted/SDIO** → join WiFi → MQTT to the dictator (`.210`) →
publish a retained "hello". No display, no camera — connectivity-first (beachhead strategy).

**Proven live 2026-07-01, first flash** — C6 SDIO link up (`Identified slave [esp32c6]`), `GOT IP
192.168.0.8`, `MQTT CONNECTED`, retained hello confirmed on the bus (`d1001-beachhead/status`).

## Build / flash
```bash
cp main/secrets.example.h main/secrets.h    # fill in WiFi creds (gitignored)
. ~/esp/esp-idf/export.sh                    # ESP-IDF v5.4 (Seeed pins 5.4.2)
idf.py set-target esp32p4                    # fetches esp_wifi_remote + esp_hosted
idf.py -p /dev/ttyACM0 flash monitor         # P4 auto-enters download mode over USB-C (no buttons)
```

## Key facts (the non-obvious bits)
- **WiFi = ESP32-C6 coprocessor over esp-hosted/SDIO.** The P4 has no radio. `esp_wifi_remote` + `esp_hosted`
  route the standard `esp_wifi_*` API to the C6; **no explicit hosted-init call** (whole-archive link
  auto-registers the transport). SDIO pins in `sdkconfig.defaults` are copied verbatim from Seeed's
  `factory_firmware` (reset=GPIO13, CLK=11 CMD=6 D0=7 D1=8 D2=9 D3=10, 4-bit @40 MHz). Init mirrors
  Espressif's `esp_wifi_remote/examples/mqtt`.
- **The C6 keeps its factory slave firmware — untouched.** We only reflash the P4. (See version-mismatch
  note below.)
- **Partition table routes AROUND the flaky 0x600000 flash sector** (found during the factory backup):
  `ota_0` @0x10000 (4 MB), gap over 0x600000, `ota_1` @0x620000 (4 MB). OTA-ready so every iteration after
  this first serial flash goes over the air.
- **Restore factory:** OEM image is off-git at `~/reterminal-d1001-factory-backup.bin`.

## Follow-ups
- **esp_hosted version mismatch:** host component is 2.12.0, C6 slave firmware is 2.3.0 — connects, but
  Espressif warns of possible RPC timeouts. Preferred fix: **pin host `esp_hosted` to ~2.3** (match the C6,
  no C6 reflash). Revisit if instability appears.
- **>16 MB internal-flash access unsupported** on this WinBond chip in the current driver — keep all
  partitions under ~11 MB (they are); the data-recovery cache uses microSD, not internal flash.
- Next (Phase 1 close): prove an actual OTA update into `ota_1`. Then Phase 2: LVGL tile renderer.
