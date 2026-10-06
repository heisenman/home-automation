# ha_aprilaire — Aprilaire E070 Remote (Model 76) RS-485 codec

Pure C frame codec for the Aprilaire 1830/1850/1870/E070 **Remote** `A`/`B` bus (ADR-0041). No UART code:
the transport is [`ha_rs485`](../ha_rs485/). Host test: `test/run.sh` (frames are live captures from our E070).

- **Wire:** 9600 8N1, `STX <ASCII body> <2-hex checksum> ETX`; checksum = (0x02 + body bytes) mod 256.
- **M-frame** (unit → remote, ~1 Hz, only while the unit's installer menu has `REMOTE` enabled):
  `M <'?' idle | '!' running> <RH hex> <error code hex>` — e.g. `M?2B03` = idle, 43 %, **E3** (remote comms loss).
- **R-frame** (remote → unit, one per M): `R <on> <dryness 1..7> <RH×10 4-hex> <temp flag> <temp>`.
  Dryness 1..7 = incoming-air dew-point setpoint 65 °F..40 °F; the unit runs when ON and its incoming air is
  above it (after a ~3-min sample; raising dryness triggers an immediate sample).

Sources: `dwrice0/aprilaire_controller` (prior art), the Model 76 installation manual (dryness, sequence of
operation, error codes), and our live capture — `docs/design/hvac-shade-device-integration.md` §2.8.
Consumer: `edge/esp32c6-dehum` (`main/aprilaire_bus.c`).
