# hvac bench self-test image

Proves the hvac/dehum node hardware (XIAO ESP32-C6 + Waveshare TTL TO RS485 (C) + relay module) on the
bench **before** it is wired to the Broan or the Aprilaire. No WiFi, MQTT or NVS. A line console on
USB-Serial-JTAG, driven by `tools/hvac_bench.py`. Pins match the node (`docs/edge-pinouts.md`).

⛔ **This image transmits on RS-485 with no listen-only gate.** Never leave it on a board that goes on the
ERV bus. Restore the node image afterwards (below).

```sh
cd edge/esp32c6-hvac/bench && idf.py set-target esp32c6 && idf.py -p /dev/ttyACM0 flash
PY=~/.espressif/python_env/idf5.4_py3.13_env/bin/python   # has pyserial
$PY tools/hvac_bench.py "relay 19 on" "relay 19 off"       # opening the port resets the chip
```

Commands: `relay <19|1> on|off` · `tx <hexbyte> <n>` · `txpin 0|1|uart` (park TX as a GPIO for metering) ·
`rxlevel` · `stats` · `safe`. Received bytes print as `RX n: ..`. A held low on the RX pin prints as
`RXPIN low for N ms`, independent of whether a valid byte forms.

## The three checks (first run: hvac_c6, 2026-10-04, all PASS)

| Check | How | Pass | Observed |
|---|---|---|---|
| Relay D8/GPIO19 | `relay 19 on/off`, meter continuity on COM–NO | beeps on, open off | ✓ |
| TX D10 → bus | `txpin 0` / `txpin 1`, meter DC volts red A+, black B- | low ≈ −1.5…−3.5 V, high positive | −3.5 V / +3.4 V |
| RX bus → D9 | `--watch 60`, touch an AA **+ to B-, − to A+** | `RXPIN low for ~N ms` each tap; reversed → nothing | ✓ both |

- **No echo.** The (C) module's receiver is off while it drives, so a `tx` never reads back. That is
  expected and is why the TX check uses a meter.
- **The meter's Ω/diode mode cannot do the RX check.** The module's bias (+3.4 V open-circuit) is too stiff
  for a meter's test current to overcome. Use a battery.
- The `00` bytes and short lows during a tap are UART break + contact bounce. They are not faults.

## Restore the node image

Cable: `cd edge/esp32c6-hvac && idf.py -p /dev/ttyACM0 flash` (NVS is untouched, so identity survives).
Then prove OTA before the node is mounted: `tools/ota_edge_node.sh hvac_c6 esp32c6-hvac v1-hvac-listen`.
