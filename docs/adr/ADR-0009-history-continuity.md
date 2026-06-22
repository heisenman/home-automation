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

## Addendum (2026-06-21): the `02` read-reject — wrap-state hypothesis (confirm/refute target)

**Observation.** The three outdoor meters (model `0x77`) split in behaviour: `living_room_outdoor`'s
buffer read cleanly via the server tool, but `attic` and `h_bed` NAK **every** read with a 1-byte `02`
at any address (arbitrary / grid-aligned / burst-from-oldest / alt-prefix). Same model → not hardware.

**Telltale.** Readable meters return **two** `0x69` metadata packets (answering both `570f3b00` and
`570f3b01`); attic/h_bed return **one**. And attic/h_bed are exactly the meters whose buffers have
**wrapped** (`oldest_ptr > newest_ptr`); living_room hadn't wrapped when it pulled.

**Hypothesis.** The history read protocol **changes once the ring buffer wraps**: a wrapped meter
collapses its metadata to one packet and expects a different read handshake. Our implementation only
speaks the **unwrapped** dialect, so wrapped meters reject it with `02`. The app handles both (its CSVs
work), so a correct wrapped-read sequence exists — we just haven't captured it.

**Sharp prediction (falsifiable).** `living_room_outdoor` will start rejecting our reads too, *once it
wraps*. If a meter that reads cleanly today flips to `02` after its buffer fills, the hypothesis is
confirmed. (Quick pre-check: is living_room's on-device log near full?)

**Crack path.** Capture an app **HCI-btsnoop of an attic or h_bed pull specifically** (a *wrapped*
meter — not living_room). Diff vs our sequence on two axes: (1) the read opcode/format — we send
`570f3c010000+addr+06`; a living_room app capture once showed `570f3c000001+addr`, so the format may
differ by wrap-state; (2) the `570f3a` / `570f3b00` / `570f3b01` setup/priming before the reads. Then
implement the wrapped-read path in `gatt_history.c` (the dedicated history path still does GATT writes,
so the v4-lockdown forwarder write-disable does not block this).

**If refuted** (the app uses the *same* sequence and it still works): fall back to a firmware/revision
variant difference, and the diff still hands us the exact bytes to match.

## RESOLVED (2026-06-22): root cause = hardcoded history-bank index, not a wrap-mode

App HCI-btsnoop of attic + h_bed pulls (decoded with `btmon`; raw in `instance/research/`, gitignored)
settles it. The outdoor read command is `570f3c [bank:1] [addr:4 BE] [count:1]`, where the byte after
`570f3c` is a **history-bank index** that must match the bank actually holding data — discovered by
probing `570f3b00..03` and taking the index whose `0x69` metadata has non-zero pointers.

- App reads **attic from bank 00** (`570f3c00…`, probed via `570f3b00`) and **h_bed from bank 03**
  (`570f3c03…`, via `570f3b03/02/01`).
- Our firmware **hardcodes bank 01** (`od_rp = 570f3c01…`, `gatt_history.c`). Bank 01 is empty on those
  meters, so the meter NAKs every read with a 1-byte `02`. `living_room_outdoor` pulled cleanly only
  because its data sat in bank 01.

**Wrap-hypothesis verdict:** directionally right, mechanism refined. Banks evidently **rotate as the
ring buffer wraps**, which is why the three outdoor meters sit in different banks — so the prediction
"`living_room_outdoor` will fail too once it wraps (into the next bank)" still stands. The fix is **not**
a wrap-specific read; it's **dynamic bank discovery**: probe `570f3b00..03`, pick the populated bank,
read `570f3c<bank>` over that bank's pointer range. (The dedicated `gatt_history.c` path still issues
GATT writes, so the v4 forwarder write-lockdown does not block this.)
