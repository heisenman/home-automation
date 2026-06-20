# ADR-0008 — Internet Weather as a Separate Data Lane

**Date:** 2026-06-20
**Status:** Accepted (internet source); Proposed (air-gapped transfer source)

## Decision

Outdoor weather (temperature, humidity, barometric pressure) is recorded by a **standalone,
modular lane** (`server/weather/`), separate from the BLE sensor pipeline:

- **Source is abstracted** (`WeatherSource`). Today: `OpenMeteoSource` (Open-Meteo, free, no
  API key, by lat/lon; zip is geocoded via zippopotam.us). Later: a transfer-reading source.
- **Sink is its own store** writing a `weather` table in the standard long format
  (`ts, source, location, metric, value, unit`), idempotent via
  `UNIQUE(source, location, ts, metric)` + `INSERT OR IGNORE`.
- **Defaults to a separate DB** (`instance/db/weather.db`), not the sensor hot tier.
- Runs as a `oneshot` service + 15-minute timer (`ha-weather.{service,timer}`); location lives
  in gitignored `instance/weather.env` (PII, like device MACs).

## Context

We want outdoor reference data to compare against locally-measured values. During dev the
system has internet, so we fetch directly. But the design goal is offline-first / eventually
air-gapped (ADR-0001): at that point weather must arrive via the **bidirectional transfer that
happens during the offline servers' backup window** — an internet-connected machine fetches
weather and syncs it in, rather than the server reaching the internet itself.

## Consequences

- The air-gap migration is a **source swap, not a rewrite**: implement a `TransferSource`
  that reads weather records synced into a known location during backup; the store, schema,
  timer, and idempotency are unchanged. `build_source()` is the only switch point.
- A separate `weather.db` is the natural unit to move in that transfer (and keeps the sensor
  hot tier clean — the compactor only manages `readings`).
- Comparison/overlay on the dashboard is a later add (DuckDB can attach `weather.db` alongside
  the sensor data); not required for recording.

## Rejected alternatives

- **Fetch weather at runtime on the air-gapped server:** violates the air-gap; the whole point
  is the server never reaches the internet.
- **Store weather in the sensor `readings` table / route via MQTT:** couples an external lane to
  the sensor pipeline; a separate table/DB keeps the transfer boundary clean and the lane
  independently swappable.
- **Cloud weather requiring an API key:** Open-Meteo is keyless, which keeps the dev path and
  any future internet-connected relay simple.
