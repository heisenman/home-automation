# ha_battery — battery gauge + thermal-gated charging (ADR-0020)

Fuel-gauge-less battery support for panel-class nodes:
- **Gauge:** ADC voltage (trimmed avg, curve-fit cali, ×divider) → SoC via a voltage LUT,
  smoothed (running avg; off-charger it only ratchets down so transients don't bounce it).
- **Temp:** board temperature from an LSM6DS3-class IMU over I2C (minimal direct-register read).
- **Charge:** enables the charger only with the cable in, cell below full, and temp in a safe
  window (fail-safe: no temp reading ⇒ no charge). A **watchdog** pulses `CHARGE_EN` to restart
  a charger IC that latched "done" while the cell is still low.

All board specifics are `ha_battery_cfg_t`. `ha_battery_d1001_cfg(io_expander, i2c_bus)` is the
reTerminal D1001 preset (ADC1 ch2/ch1 ×2, PCA9535 read-en pin6 / charge-en pin10, IMU @0x6A on
I2C-1, GPIO15 charge / GPIO4 PG, 21-pt LUT, 2..43 °C, stop 4150 mV / recharge 4050 mV).

## Platform support
Any node whose battery sense is ADC + (optional) PCA9535-gated charge/read-enable + (optional)
I2C IMU temp. The expander/I2C handles are injected — this component does not own them.

## Consumed by
- d1001-panel (reTerminal D1001) — first adopter.
