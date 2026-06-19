# Changelog

All notable changes are documented here.
Format: [ISO date] — description (ADR reference if applicable)

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
