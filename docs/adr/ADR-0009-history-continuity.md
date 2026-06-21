# ADR-0009 — History Continuity (Relay-Primary, Buffer-Pull for Gaps)

**Date:** 2026-06-20
**Status:** Accepted

## Decision

Sensor history continuity is maintained **primarily by continuous live relay** — edge nodes and the
server scanner persist every advertised reading in real time. The meters' on-device **ring buffer is
a gap-recovery mechanism, not the primary data path.** Gaps from node downtime are recovered, in order
of preference: (1) uptime + redundancy so gaps stay minimal; (2) a wrap-aware autonomous BLE buffer
pull (medium-term); (3) SwitchBot app CSV export (manual backstop, works offline).

## Context

SwitchBot meters store on-device history in a **circular ring buffer**, pulled over an undocumented,
reverse-engineered BLE protocol (ADR-0007). Two quirks make full buffer pulls unreliable for
long-logging meters:

- **Wrap:** once a meter has logged past its buffer size, the newest record's address is *lower* than
  the oldest's (the ring wrapped), so naive oldest→newest paging (assuming `newest_ptr > oldest_ptr`)
  reads nothing or the wrong range.
- **Single-pointer / clock drift:** screenless meters' clocks drift (see `reanchor_to_now`). The
  device returns only the *oldest* pointer to our tool unless the handshake carries a time matching its
  drifted clock; the app sends a matching time and gets all pointers (newest + wrap + oldest), our tool
  (so far) does not.

These blocked clean BLE backfill of h_bed, attic, and c_office. They were instead backfilled from the
**SwitchBot app's CSV export** (≈5 months/meter, per-minute), which works regardless of buffer state
and even offline. Crucially, **live relay already captures every reading as it is advertised**, so the
on-device buffer is unnecessary for ongoing data.

## Consequences

- The wrap is **moot for current data** — relay streams it continuously to the DB.
- The buffer pull is needed **only to fill gaps** during relay downtime; redundancy (multiple nodes
  hearing each meter, the failover server, node watchdogs) shrinks that need toward zero.
- The **wrap-aware pull** is a medium-term, well-scoped upgrade for autonomous self-healing:
  detect `newest_ptr < oldest_ptr` and page across the wrap (oldest→buffer_max, then buffer_min→newest);
  send a handshake time matching the device clock so it returns the newest pointer. Its value grows
  because **every meter eventually wraps** (the ones that pull cleanly today will too).
- **CSV export** is a proven manual backstop, valid even air-gapped (the app pulls over local BLE,
  no internet). Format/timezone handling lives in `tools/import_switchbot_csv.py` (idempotent).

## Rejected alternatives

- **Buffer pull as the primary path:** fragile (wrap, clock quirks, undocumented) and unnecessary
  given continuous live relay.
- **Cloud history:** SwitchBot's cloud API has no history endpoint (ADR-0007); violates offline-first.
- **Routine CSV export:** manual; doesn't fit the autonomous/air-gapped goal — acceptable only as a
  backstop for large one-time recoveries.
