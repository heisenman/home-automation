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

## Finding (2026-06-20): SwitchBot has NO device-side BLE history

Investigated whether meters can be queried directly over BLE for their stored log.
**They cannot.** Confirmed three ways:
- SwitchBot's own BLE spec (`SwitchBotAPI-BLE/devicetypes/meter.md`) documents only one
  read command, `0x31` = *current* temperature/humidity. There is no historical-log command.
- `pyswitchbot` (the mature reference lib, used by Home Assistant) exposes **no** history/
  log/fetch methods for meters — only current-reading classes.
- No community tool implements it; the consistent advice is "log readings yourself."

Implication: the meters do not retain a long log on-device. The phone app's multi-month
graphs (and the original 5-month CSV) come from the **SwitchBot cloud**, which the app
continuously syncs to — not from a device-side log. So:
- **SwitchBot "history sync" over BLE is not possible.** Our continuous scanner IS the
  historian for SwitchBot devices; gap-minimization (scanner liveness watchdog + the MQTT
  bridge) is the mitigation, not a device backfill.
- Pre-existing SwitchBot history can only come from the **cloud** one-time
  (`tools/import_switchbot_history.py`, the "walled cloud lane"), if the v1.1 cloud API
  exposes meter time-series (unverified). Not part of runtime.

## History-sync implementation notes — Aranet only (the case that works)

- **Aranet** genuinely stores a long on-device log and exposes it over BLE. The open-source
  `aranet4` library (Anrijs/aranet4) pulls the full log (temp/humidity/CO₂/pressure/radon
  with timestamps, weeks–months) over a GATT connection. For the Radon Plus this also
  sidesteps the foil-subfloor advertisement problem — a periodic connection from the
  crawlspace ESP32 can dump the whole log regardless of advertisement range.
- **Shape:** connect → read records newer than `MAX(ts)` we hold → decode → `INSERT OR
  IGNORE` → disconnect. Bounded retry; never block the live scanner. Needs the dedicated
  dongle (connection-based, heavier on the radio than passive scanning).
- **Cadence:** run inside the device's retention window (Aranet weeks-to-months by interval).

## Rejected alternatives

- **Backfill only via manual phone-app re-export + CSV import:** works (and remains the
  fallback), but requires a human and the proprietary app; not self-sufficient.
- **Larger on-disk buffering / sensor-side replay:** impossible — BLE advertisements have
  no retransmit; the device log over a connection is the only recovery channel.
- **Application-level dedup before insert:** fragile and racy; a DB uniqueness constraint
  is the correct structural guarantee.
