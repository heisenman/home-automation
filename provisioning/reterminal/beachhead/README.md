# D1001 host beachhead (ADR-0019 Phase 1 + 2)

ESP-IDF app that **proves the connectivity + OTA stack** on the reTerminal D1001's ESP32-P4, then brings up
the **display** on demand: boot → bring up the **ESP32-C6 over esp-hosted/SDIO** → join WiFi → MQTT to the
dictator (`.210`), then (on the `cmd/display on` command) → **JD9365 800×1280 MIPI-DSI panel + LVGL**.
Camera stays off. Carries permanent, WiFi-toggled **remote-debug-over-MQTT** tooling (see below).

**Phase 1 PROVEN live 2026-07-01, first flash:**
- **Connectivity** — C6 SDIO link up (`Identified slave [esp32c6]`), `GOT IP 192.168.0.8`, `MQTT CONNECTED`.
- **OTA** — full over-the-air update proven: `ota begin→connected→progress→complete`, device flipped
  `ota_0`→`ota_1` running the new build, verified on the bus. All further iteration is wireless.

**Phase 2 PROVEN live 2026-07-01 (v9-disp):** panel lit + LVGL splash rendered on real hardware.
- **Display is COMMAND-TRIGGERED, not auto-at-boot** (`cmd/display on`). This is deliberate: WiFi/MQTT come
  up *first* and mark the app valid, so a failed bring-up costs only one reboot back into the same good
  firmware — never a brick. Bring-up is non-fatal (`bsp_display.c` returns errors, never aborts).
- Lean driver path (`bsp_display.c`): PCA9535 power rails → DSI-PHY LDO(ch3,2500mV) → 2-lane DSI @1Gbps →
  JD9365 (reset via expander) → LEDC backlight(GPIO14) → `esp_lvgl_port` DSI display. Drops the Seeed BSP's
  esp-sr/codec/cam/IMU/RTC — app is ~1.4 MB, 66% of the 4 MB OTA slot free.
- **Bootloader rollback now ENABLED** (`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`): a future OTA that never
  reaches MQTT auto-reverts to the last good slot. `esp_ota_mark_app_valid` fires on MQTT connect.

## ⚠️ Two P4 gotchas that cost real time (remember for E1001 + every future panel)
- **200 MHz PSRAM needs `CONFIG_IDF_EXPERIMENTAL_FEATURES=y`.** On P4, `SPIRAM_SPEED_200M` *depends on* the
  experimental flag; without it Kconfig silently falls back to the **20 MHz** default — far too slow to scan
  out an 800×1280 DPI framebuffer. Set BOTH `IDF_EXPERIMENTAL_FEATURES=y` and `SPIRAM_SPEED_200M=y`.
- **esp-hosted SDIO mempool must go to PSRAM once you link LVGL.** Linking the display stack grows internal
  `.bss`; the esp-hosted SDIO buffer pool is DMA-capable-internal by default and OOMs at boot
  (`sdio_mempool_create ... assert failed`, boot-loop). Fix: `CONFIG_ESP_HOSTED_MEMPOOL_PREFER_SPIRAM=y`
  (+ `CONFIG_ESP_HOSTED_DFLT_TASK_FROM_SPIRAM=y`) — P4 GDMA reaches PSRAM, so the pool works fine there.

## Vendored components (NOT in the Espressif registry)
The panel/touch/expander drivers are Seeed-local components (path-based, not registry packages), so they are
**gitignored** here. Before building, copy them from the Seeed reTerminal-D1001 clone:
```bash
SEEED=~/reterminal-dev/reTerminal-D1001/components
mkdir -p components
for c in esp_lcd_jd9365_8 esp_lcd_touch_gsl3670 esp_io_expander_pca9535; do cp -r "$SEEED/$c" components/; done
```
Their own manifests pull the registry deps they need (`esp_lcd_touch`, `esp_io_expander`, `cmake_utilities`).
Note: `esp_lcd_touch_gsl3670` `extern`s a global `io_expander` handle for touch-reset — `bsp_display.c`
defines it (non-static) and treats the touch `rst_gpio_num` as an **expander pin** (12), not a real GPIO.

## Build / flash
```bash
cp main/secrets.example.h main/secrets.h    # fill in WiFi creds (gitignored)
. ~/esp/esp-idf/export.sh                    # ESP-IDF v5.4 (Seeed pins 5.4.2)
idf.py set-target esp32p4                    # fetches esp_wifi_remote + esp_hosted
idf.py -p /dev/ttyACM0 flash                 # P4 auto-enters download mode over USB-C (no buttons)
```

## Key facts (the non-obvious bits)
- **WiFi = ESP32-C6 coprocessor over esp-hosted/SDIO.** The P4 has no radio. `esp_wifi_remote` + `esp_hosted`
  route the standard `esp_wifi_*` API to the C6; **no explicit hosted-init call** (whole-archive link
  auto-registers the transport). SDIO pins in `sdkconfig.defaults` are copied verbatim from Seeed's
  `factory_firmware` (reset=GPIO13, CLK=11 CMD=6 D0=7 D1=8 D2=9 D3=10, 4-bit @40 MHz). Init mirrors
  Espressif's `esp_wifi_remote/examples/mqtt`.
- **The C6 keeps its factory slave firmware — untouched.** We only reflash the P4.
- **Partition table routes AROUND the flaky 0x600000 flash sector** (found during the factory backup):
  `ota_0` @0x10000 (4 MB), gap over 0x600000, `ota_1` @0x620000 (4 MB). OTA writes never touch the bad sector.
- **⚠️ OTA-over-HTTP gotcha:** `esp_https_ota` **rejects a plain `http://` URL** with `ESP_ERR_INVALID_ARG`
  ("No option for server verification is enabled…") unless you set **`CONFIG_ESP_HTTPS_OTA_ALLOW_HTTP=y`**
  (or supply a TLS cert). This was the entire reason early OTA attempts made no HTTP request. Fixed in
  `sdkconfig.defaults`. Every ESP-IDF OTA-over-LAN hits this — remember it for the E1001 + future panels.
  (Production hardening later = signed images / TLS, not plain HTTP.)
- **Restore factory:** OEM image is off-git at `~/reterminal-d1001-factory-backup.bin`.

## Remote debug over MQTT (permanent tooling — how to debug a headless ESP with no serial)
Serial console is unavailable in the headless build host, so the firmware **streams its own diagnostics over
the bus**. It's compiled in permanently and **default OFF** (near-zero cost); flip it on over WiFi when
needed. This is the reusable pattern for the whole fleet.

| Topic | Dir | Purpose |
|---|---|---|
| `d1001-beachhead/status` | ← | retained heartbeat every 15 s: `{partition,build,ip,uptime_s,heap,rssi,wifi_rc,mqtt_rc,debug}`; retained **LWT `offline`** on unexpected drop → instant liveness/health |
| `d1001-beachhead/ota` | ← | OTA lifecycle (`begin/connected/progress/complete/fail`+err) — **always** published |
| `d1001-beachhead/log` | ← | full esp-idf log firehose — **only when debug on** |
| `d1001-beachhead/ack` | ← | echoes any command topic received (proves receipt) |
| `d1001-beachhead/cmd/debug` | → | `on`/`off` — toggle the log firehose (default off) |
| `d1001-beachhead/cmd/ota` | → | payload = http URL of the new `.bin` |
| `d1001-beachhead/cmd/ping` | → | force an immediate status publish (poll liveness) |
| `d1001-beachhead/cmd/display` | → | `on` — trigger display bring-up (default off; safe to retry) |
| `d1001-beachhead/display` | ← | retained bring-up result `{display:online|failed, panel, res, err}` |

```bash
# watch everything:
mosquitto_sub -h 192.168.0.210 -t 'd1001-beachhead/#' -v
# turn on the log firehose, then trigger an OTA:
mosquitto_pub -h 192.168.0.210 -t d1001-beachhead/cmd/debug -m on
mosquitto_pub -h 192.168.0.210 -t d1001-beachhead/cmd/ota -m "http://<host>:8000/d1001_beachhead.bin"
```
Implementation notes: `esp_log_set_vprintf` hook only **enqueues** (never publishes inline → no recursion);
a drain task publishes; noisy components (`mqtt_client`/`transport`/`esp-tls`) forced to WARN. The OTA HTTP
server must be reachable from the panel's subnet (open the host firewall, e.g. `sudo ufw allow 8000/tcp`).

## Follow-ups
- **esp_hosted version mismatch:** host 2.12.0 vs C6 slave 2.3.0 — connects, but Espressif warns of possible
  RPC timeouts. Preferred fix: **pin host `esp_hosted` to ~2.3** (match the C6, no C6 reflash). Now safer to
  attempt: bootloader rollback is enabled (a bad OTA reverts). Revisit if instability appears.
- **>16 MB internal-flash access unsupported** on this WinBond chip in the current driver — keep partitions
  under ~11 MB (they are); the recovery cache uses microSD, not internal flash.
- **DONE — bootloader rollback enabled** (`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`). Requires the rollback
  bootloader, installed via a full USB flash (OTA doesn't rewrite the bootloader).
- **Next: Phase 2 tiles — the server-backed LVGL renderer** (ADR-0019 §4): now that the panel renders, pull
  live tiles from the BFF + MQTT (fully usable with no SD card), wire touch→signed commands, then the
  declarative UI manifest. The SD data-agent stays an optional presence-gated add-on.
