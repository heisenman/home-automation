# Changelog

All notable changes are documented here.
Format: [ISO date] — description (ADR reference if applicable)

## 2026-06-19 — BLE decoder fixes + historical import

**Bugs fixed:**
- Mosquitto drop-in config: removed duplicate `persistence_location` (conflict with `/etc/mosquitto/mosquitto.conf` default)
- BLE scanner: passive mode → active mode (passive requires BlueZ `or_patterns`, not set)
- SwitchBot decoder: Outdoor Meter (Format B) prefixes manufacturer data with 6-byte MAC — old code read `mfr[1:]` landing on MAC bytes, producing temperatures like -65°C. Fixed to `mfr[6:]`. Added model byte `0x77` ('w') for `switchbot_meter_outdoor`.

**Historical data import:**
- 8,311,420 rows from SwitchBot app CSV export (Jan–Jun 2026, 1-min resolution)
- 10 devices: master_bedroom, c_office, living_room (×2), c_bedroom, h_bathroom, h_bedroom, kitchen, master_bathroom, attic
- Stored in `instance/db/hot.db` (~2 GB). Run compactor to flush to Parquet.
- Import tool: `tools/_run_import.py` (stdlib-only, runs without venv)

**Status at end of session:**
- Scanner running, SwitchBot Outdoor Meters decoding correctly (verified: 74°F house → 23.3–23.7°C readings match)
- `instance/devices.yaml` MACs not yet populated — use `mosquitto_sub -h localhost -t 'home/#' -v` to find them
- Aranet not yet seen on BLE — confirm Smart Home Integration is enabled on device
- Compactor not yet run on live data

## 2026-06-19 — Initial build

- Monorepo scaffold: `server/`, `docs/adr/`, `systemd/`, `config-examples/`, `tools/`
- BLE ingest: passive scanner for SwitchBot Meter Pro (manufacturer 0x0969 / service fd3d)
  and Aranet Radon Plus (service fce0, Smart Home Integration broadcast mode)
- MQTT message contract v1 (§7 of architecture plan)
- Hot SQLite writer: long-format `readings` table, WAL mode
- Parquet compactor: daily flush with Zstd, summary tier, hash manifest
- FastAPI query service: summary endpoint + bounded DuckDB deep-dive
- Systemd service units: ha-scanner, ha-writer, ha-api + daily ha-compactor timer
- Device registry schema (YAML); example config committed; real instance/ git-ignored
