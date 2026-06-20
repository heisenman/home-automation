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

## Finding (2026-06-20): SwitchBot DOES have device-side BLE history (undocumented)

First investigation wrongly concluded SwitchBot meters have no BLE history, reasoning from
"undocumented + no library implements it." That inference was wrong. Corrected by:
- **SwitchBot's own support docs:** meters store the recent **36 days** (Meter, Meter Pro CO2)
  or **68 days** (Meter Plus, Outdoor Meter, Meter Pro) of temp/humidity *on the device*,
  "transferred via Bluetooth" when the app opens the history graph.
- **Empirical:** pulling old data in the app fails on *Bluetooth connectivity* and is
  interrupted by household *EMI* (shop-vac) — the signature of a BLE bulk transfer, not a
  cloud fetch (which would use WiFi and ignore RF noise).

So the on-device log is real and BLE-pullable (our devices: 68 days each). The catch is only
that the history command is **not in SwitchBot's public BLE spec** (`meter.md` lists just
`0x31` = current readings) and **no library has reverse-engineered it** — "undocumented",
not "nonexistent."

Backfill value: up to **68 days** of gap recovery per device, fully offline, no cloud. This
makes a SwitchBot history fetcher worth building after all.

## Build path — reverse-engineer the undocumented history command

1. **Capture** the SwitchBot Android app's BLE traffic while it pulls a meter's history
   (Android "Bluetooth HCI snoop log" → `btsnoop_hci.log`, via a bug report or `adb`).
2. **Decode** the GATT exchange on the SwitchBot custom service (`cba20d00-…`): the write to
   the command char (`cba20002-…`) that requests the log, and the notification stream on
   (`cba20003-…`) that returns it. Identify the request opcode + the record framing/epoch.
3. **Implement** `tools/switchbot_history.py`: connect → request records newer than `MAX(ts)`
   we hold → decode → `INSERT OR IGNORE` → disconnect. Per-model validation against real bytes
   like the live decoder. Needs the dedicated dongle (connection-based, heavier on the radio).

## Aranet history — the easy case (library exists)

- **Aranet** also stores a long on-device log and exposes it over BLE, but unlike SwitchBot
  it's documented and implemented: the `aranet4` library (Anrijs/aranet4) pulls the full log
  (temp/humidity/CO₂/pressure/radon, weeks–months) over GATT. For the Radon Plus this also
  sidesteps the foil-subfloor advertisement problem — a periodic connection dumps the whole
  log regardless of advertisement range. Lower effort than SwitchBot (no RE needed).

## Shared shape & cadence (both device families)

- connect → read records newer than `MAX(ts)` → decode → `INSERT OR IGNORE` → disconnect;
  bounded retry; never block the live scanner.
- Run inside each device's retention window (SwitchBot 36–68 days; Aranet weeks–months).

## Rejected alternatives

- **Backfill only via manual phone-app re-export + CSV import:** works (and remains the
  fallback), but requires a human and the proprietary app; not self-sufficient.
- **Larger on-disk buffering / sensor-side replay:** impossible — BLE advertisements have
  no retransmit; the device log over a connection is the only recovery channel.
- **Application-level dedup before insert:** fragile and racy; a DB uniqueness constraint
  is the correct structural guarantee.
