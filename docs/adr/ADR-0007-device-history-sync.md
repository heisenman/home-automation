# ADR-0007 — Idempotent Ingestion & Device History Sync

**Date:** 2026-06-19
**Status:** Accepted (idempotent ingestion); Proposed (history sync, post-migration)

## Decision

1. **Idempotent ingestion (implemented now).** The `readings` table carries a
   `UNIQUE(device_id, ts, metric)` index, and every write path (live writer, bulk CSV
   importer, future history sync) uses `INSERT OR IGNORE`. Re-ingesting an overlapping
   time range can never create duplicate rows. This is the same key the compactor dedups
   on (ADR-0006).

2. **Device history sync (planned, after the BT-dongle migration).** Add a scheduled
   `ha-history-sync` job (sibling to the compactor timer) that pulls each device's
   *internal log* over a BLE GATT connection — the same mechanism the vendor phone apps
   use — and idempotently inserts anything newer than our last record per device.

## Context

Our live capture is passive BLE advertisement scanning. Advertisements are ephemeral: any
window where the scanner isn't listening (BLE adapter glitch, restart, radio contention)
is a permanent gap in *our* database. We hit several such gaps during bring-up.

But the sensors log readings internally — that is how the SwitchBot phone app exported ~5
months of history at project start. So a gap in our DB is recoverable from the device's
own log, retrieved over a connection, provided we sync within the device's retention
window. Making ingestion idempotent is the prerequisite: a history pull re-fetches
overlapping ranges by design, so duplicate suppression must be structural, not careful
timing.

## Consequences

- Re-imports and history pulls are safe to run repeatedly; the DB self-converges.
- The live writer is also protected against double-inserting retained MQTT messages on
  reconnect (this was a real source of duplicates — 801 groups found at migration time).
- Gaps become self-healing at the *data* layer, not just the scanner-liveness layer:
  a missed window backfills on the next history sync.
- Eventually removes the dependency on the vendor phone app for history.

## History-sync implementation notes (for the post-migration build)

- **Aranet (do first — low effort, high value):** the open-source `aranet4` library
  (Anrijs/aranet4) pulls the full on-device log (temp/humidity/CO₂/pressure/radon with
  timestamps) over a GATT connection. For the Radon Plus this also sidesteps the
  foil-subfloor advertisement problem entirely — a nightly connection from the crawlspace
  ESP32 can dump the whole log regardless of advertisement range.
- **SwitchBot (later — more work):** meters expose stored history over their GATT service;
  protocol reverse-engineered in `SwitchBotAPI-BLE` / `pySwitchbot`. Per-model formats and
  retention differ (Meter Pro stores more than the basic Meter); each needs validation
  against real bytes like the live decoder did. Connection-based fetch is heavier on the
  radio — schedule it after the dedicated dongle exists, not on the shared AX210.
- **Shape:** per device → connect → request entries newer than `MAX(ts)` we hold → decode →
  `INSERT OR IGNORE` → disconnect. Bounded retry; never block the live scanner.
- **Cadence:** must run inside each device's on-device retention window (SwitchBot ~days at
  fine resolution; Aranet weeks-to-months by interval).

## Rejected alternatives

- **Backfill only via manual phone-app re-export + CSV import:** works (and remains the
  fallback), but requires a human and the proprietary app; not self-sufficient.
- **Larger on-disk buffering / sensor-side replay:** impossible — BLE advertisements have
  no retransmit; the device log over a connection is the only recovery channel.
- **Application-level dedup before insert:** fragile and racy; a DB uniqueness constraint
  is the correct structural guarantee.
