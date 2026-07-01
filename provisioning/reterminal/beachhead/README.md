# D1001 host beachhead (ADR-0019 Phase 1)

ESP-IDF app that **proves the connectivity + OTA stack** on the reTerminal D1001's ESP32-P4 before any UI:
boot → bring up the **ESP32-C6 over esp-hosted/SDIO** → join WiFi → MQTT to the dictator (`.210`). No
display, no camera — connectivity-first (beachhead strategy). Now also carries permanent, WiFi-toggled
**remote-debug-over-MQTT** tooling (see below).

**Phase 1 PROVEN live 2026-07-01, first flash:**
- **Connectivity** — C6 SDIO link up (`Identified slave [esp32c6]`), `GOT IP 192.168.0.8`, `MQTT CONNECTED`.
- **OTA** — full over-the-air update proven: `ota begin→connected→progress→complete`, device flipped
  `ota_0`→`ota_1` running the new build, verified on the bus. All further iteration is wireless.

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
  RPC timeouts. Preferred fix: **pin host `esp_hosted` to ~2.3** (match the C6, no C6 reflash). Enable
  bootloader rollback before risking a version change over OTA. Revisit if instability appears.
- **>16 MB internal-flash access unsupported** on this WinBond chip in the current driver — keep partitions
  under ~11 MB (they are); the recovery cache uses microSD, not internal flash.
- **Next: Phase 2 — the server-backed LVGL renderer** (ADR-0019 §4 refinement): the panel UI pulls from the
  BFF + MQTT and is **fully usable with no SD card**; the SD data-agent is an optional presence-gated add-on.
