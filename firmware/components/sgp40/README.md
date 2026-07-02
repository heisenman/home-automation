# sgp40 — Sensirion SGP40 VOC gas sensor I2C driver (ADR-0020)

Minimal ESP-IDF (new `i2c_master`) driver for the Sensirion SGP40 air-quality sensor:
- **`sgp40_measure_raw()`** — one raw VOC signal (SRAW, ~30 ms) with optional RH/T compensation
  (`sgp40_rh_ticks()` / `sgp40_t_ticks()` convert real values; `SGP40_DEFAULT_RH/_T` = uncompensated).
- **`sgp40_self_test()`** — on-chip self-test; doubles as a live wiring check (returns an I2C error if
  SDA/SCL/VCC aren't connected).
- **`sgp40_heater_off()`** / **`sgp40_serial()`** — idle the hotplate (low-duty only) / read the 48-bit id.

Pure I2C transport — **no secrets, no node policy, no bus ownership**. The caller creates the
`i2c_master_bus_handle_t` and injects it into `sgp40_init(&s, bus, scl_hz)`. Address `0x59`, CRC-8
(poly 0x31) validated on every word.

## VOC Index
The raw SRAW is device-specific and drifts; convert it to a comparable **0..500 VOC Index** (100 =
baseline) by feeding each 1 Hz sample to the [`sensirion_gas_index`](../sensirion_gas_index/) component.
That algorithm requires a **fixed 1 Hz cadence** and a ~45 s warm-up (outputs 0 during blackout).

## Platform support
Any node with an I2C bus (edge c3/c6/s3, reTerminal panel). Board wiring — bus, SDA/SCL pins, SCL
speed — is the caller's; this component only speaks to the chip.

## Consumed by
- `c6-bench` edge node via [`ha_gas`](../../edge/esp32c6/main/ha_gas.c) (dual-role: BLE relay + VOC).
