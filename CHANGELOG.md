# Changelog

All notable changes are documented here.
Format: [ISO date] — description (ADR reference if applicable)

## 2026-06-19 — Device confirmation, battery fix, passive scan, LAN API, dashboard

**Device registry — all 10 SwitchBots positively identified:**
- Confirmed by app readings, Meter Pro paired display, and breathe-test (warm/humidify a
  sensor, observe which MAC spikes). master_bath nailed via breathe test (AA:BB:CC:00:00:04).
- h_bed/h_bath and master_bath/c_bed/living_room corrected from initial RSSI guesses.

**Battery decode fixed (was producing 2% / 118% / 122% garbage):**
- Battery is in service-data byte 2 (documented fd3d layout), NOT the manufacturer-data
  byte after the MAC (that's a status/flags field). All devices now read a sane 99–100%,
  matching the hardware displays. Verified by replaying captured advertisement bytes.
- Added Meter Pro model byte 0x34 (newer firmware) to the model map.

**Passive BLE scanning (fixes wireless-mouse drops):**
- Active scanning on the shared AX210 controller starved the Bluetooth mouse (Razer
  Basilisk) on the same radio. Switched to passive scanning with BlueZ or_patterns
  (SwitchBot 0x0969 / fd3d, Aranet fce0) — listen-only, low radio load. Default HA_SCAN_MODE=passive.

**Scanner watchdog (fixes 2-minute crash loop):**
- Service was Type=simple with WatchdogSec=120 but never pinged systemd → killed every 2 min,
  which also reset the BLE radio and dropped the mouse. Added sd_notify READY/WATCHDOG loop,
  Type=notify. Stable since.

**Raw-publish debounce:** decode-fail raw messages now rate-limited to 1/60s per device (was flooding).

**Compactor fixes (8.3M rows → 22 MB Parquet, hot.db 1.9 GB → 1.5 MB):**
- DELETE by timestamp range instead of an 8.3M-element IN() list (SQLite var limit).
- read_parquet with explicit schema to avoid partition-column schema conflict on re-run.
- Dedup via DuckDB window function instead of pandas (pandas not in venv).

**Web dashboard + LAN access:**
- Added GET / dashboard (auto-refresh, °F, humidity, battery, stale flag) to the API.
- API bind address configurable via HA_BIND_HOST, default 0.0.0.0 (LAN-reachable at :8123).
  Read-only, unauthenticated — trusted-LAN only.

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
