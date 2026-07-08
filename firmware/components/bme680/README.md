# bme680 — Bosch BME680 driver (ESP-IDF, new i2c_master)

Shared firmware component (ADR-0020). Pure I2C transport **plus the Bosch compensation baked in**: the
BME680 returns raw ADC + per-chip calibration, and this component turns that into physical units —
temperature (°C), humidity (%RH), pressure (hPa), and gas resistance (Ω, via the two Bosch range LUTs).
Forced-mode single-shot; addr 0x76 (SDO→GND) / 0x77 (SDO→VCC); chip id 0x61.

Not included: Bosch's calibrated IAQ index (BSEC) — a closed binary blob with no clean RISC-V/offline
path. The honest air-quality signal is `gas_resistance_ohm` (higher = cleaner air). An IAQ index can be
derived server-side from a running gas-resistance baseline if wanted.
