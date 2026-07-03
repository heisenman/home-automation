# Edge-node pinouts — downstream device wiring (I²C / SPI)

**Purpose.** One place to look up where a downstream sensor (I²C/SPI) wires on each edge board, and where in
firmware those pins are defined. When a build's pins change, update this doc *and* the cited source line.

> Rule of thumb: the firmware **bus-scan logs the SDA/SCL it's using at boot** (`SGP40 bus scan (SDA=GPIO..
> SCL=GPIO..)`), so a serial monitor is always the ground truth. This doc saves you the reflash.

## Quick reference

| Board | Build dir | I²C SDA | I²C SCL | I²C clk | SPI (onboard) | RGB LED | Pin source |
|---|---|---|---|---|---|---|---|
| **ESP32-S3-ETH** (Waveshare) | `edge/esp32s3-eth` | **GPIO42** | **GPIO41** | 400 kHz | W5500 Ethernet on **9–14** (reserved) | WS2812 **GPIO21** | `main/ha_gas.c`, `main/ha_eth.c`, `main/ha_led.h` |
| **XIAO ESP32-C6** (Seeed) | `edge/esp32c6` | **GPIO22** (D4) | **GPIO23** (D5) | 400 kHz | — (WiFi-only) | — | `main/ha_gas.c` |
| **XIAO ESP32-C3** (Seeed) | `edge/esp32c3` | *GPIO6 (D4)†* | *GPIO7 (D5)†* | — | — | — | *none yet — BLE-only* |

† C3 has **no I²C sensor lane in firmware yet** (it's a BLE-scanner/GATT-history node). GPIO6/7 are the Seeed
XIAO-C3 default I²C pads — **verify against the board** when you add a sensor lane, then define the pins in a
new `edge/esp32c3/main/ha_gas.c` (or similar) and update the row above.

## Per-board detail

### ESP32-S3-ETH (Waveshare) — `edge/esp32s3-eth`
- **I²C (downstream sensors):** SDA=`GPIO42`, SCL=`GPIO41`, 400 kHz, `I2C_NUM_0` — `main/ha_gas.c:18`.
  ⚠️ The ESP32-S3 has **no GPIO22/23** (unlike the C6) — don't copy C6 wiring here.
- **SPI (onboard W5500 Ethernet, `SPI2_HOST`, 20 MHz)** — `main/ha_eth.c`: MOSI=`11`, MISO=`12`, SCLK=`13`,
  CS=`14`, INT=`10`, RST=`9`. **GPIO 9–14 are reserved** for Ethernet; keep sensors clear of them.
  (Firmware falls back to WiFi if no W5500 is present — a plain S3 works too, see `v18-wifi-fallback`.)
- **RGB status LED:** onboard WS2812 on `GPIO21` — `main/ha_led.h`.
- **Power for Grove/downstream:** 3V3 + GND. Grove SGP-40 carries its own pull-ups; firmware also enables
  internal pull-ups (belt-and-braces).

### XIAO ESP32-C6 (Seeed) — `edge/esp32c6`
- **I²C:** SDA=`GPIO22` (silk **D4**), SCL=`GPIO23` (silk **D5**), 400 kHz, `I2C_NUM_0` — `main/ha_gas.c:16`.
- **No SPI / no Ethernet** — WiFi-only BLE-relay (+ optional I²C gas) node.
- **Power:** 3V3 + GND. The XIAO's 3V3 pin powers a Grove sensor fine.

### XIAO ESP32-C3 (Seeed) — `edge/esp32c3`
- BLE scanner / GATT-history node; **no I²C sensor lane defined yet.** When adding one, use the XIAO-C3 I²C
  pads (default D4=`GPIO6` SDA, D5=`GPIO7` SCL — verify), add the pin defines + bus-scan, and update this doc.

## Downstream sensor I²C addresses

| Sensor | Address | Metrics | Firmware component |
|---|---|---|---|
| Sensirion **SGP40** | **0x59** | VOC index (from raw SRAW, on-node Sensirion Gas Index Algorithm) | `firmware/components/{sgp40, sensirion_gas_index}` (ADR-0020, modular) |
| Sensirion **SGP30** | **0x58** | eCO₂ (ppm) + TVOC (ppb) | `firmware/components/sgp30` (planned — modular, same pattern) |

> SGP30 (0x58) and SGP40 (0x59) are **different chips with different command sets** — the SGP40 driver will
> NOT read an SGP30. A board's bus-scan will ACK 0x58 (SGP30) or 0x59 (SGP40); pick the matching driver.

## Related
- Firmware module matrix: `edge/MATRIX.md`. Modular-component policy: ADR-0020.
- Per-node gas registry entries (synthetic `<node>-gas` reg key → device_id): `instance/devices.yaml` (gitignored).
