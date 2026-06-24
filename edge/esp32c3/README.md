# ESP32-C3 edge node — Wi-Fi BLE-relay for the dictator

A **Wi-Fi** BLE-scan→relay node, forked from `edge/esp32c6/` (the closest base: also Wi-Fi-only, single
shared 2.4 GHz radio, same NimBLE coex story). Passive-scans SwitchBot (and other) BLE adverts and relays
them to the dictator's MQTT broker. Same relay contract as the other nodes:
`home/edge/<node>/<mac>/adv` → `ha-edge-mapper` → `home/<area>/<device>/state`.

## Why a fork of `esp32c6/`
Identical firmware modules — `ble_scan`, `switchbot_decode`, `ha_mqtt`, `ha_config`, `ha_sntp`, `gatt_*`,
`ha_ota`, `ha_wifi`. The **only** change vs the C6 is the build target (`esp32c3`) in `sdkconfig.defaults`.
The ESP32-C3 is single-core RISC-V with ~400 KB SRAM; the image builds with ~28 % app-partition headroom.

## Bring-up (one shot, gated)
No Ethernet on this board, so it's Wi-Fi only — duty-cycle coex is always on. Use the bring-up tool:
```bash
python3 tools/node_bringup.py edge/esp32c3 <node-id> --mac <chip MAC> --target esp32c3 \
    --port /dev/ttyACM0 --broker mqtt://192.168.0.210:1883 --ota-host 192.168.0.210 --serve-ip 192.168.0.210
#   ENROLL → BUILD → FLASH → VERIFY-RELAY → BENCH-OTA, each gated.
```
Broker = the box IP (`.210`) on a Wi-Fi segment that can't ARP the VIP `.200` (FIRMWARE-GUIDE §3.6); use the
VIP on a segment that can reach it.

## Board-specific TODO (confirm before/at flash)
- **RGB error LED** (`ha_led`, off-by-default + error codes — see the S3 node): not yet ported here because the
  C3 board's WS2812 GPIO is board-specific (many C3 devkits use GPIO8, a strapping pin — confirm the schematic).
  Port `ha_led.{c,h}` from `edge/esp32s3-eth/main/` and set `LED_GPIO` once the exact board is known.
- **Flash size**: defaults to 4 MB (two ~1.75 MB OTA slots). Confirm the module has ≥4 MB.
- Pull the chip MAC with `esptool.py -p /dev/ttyACM0 read_mac` for enrollment.

Status: firmware fork **builds clean** for `esp32c3`; flash + verify-relay + bench-OTA pending a physical C3
board on USB.
