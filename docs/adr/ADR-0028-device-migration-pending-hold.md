# ADR-0028 — Device migration retirement: the pending-hold state

**Date:** 2026-07-08
**Status:** **Accepted** (Hugh, 2026-07-08). Dev-authored while migrating devices onto the air-gap network
(ADR-airgap / `docs/airgap/MIGRATION-DESIGN-LOG.md`); surfaced by real stale-ghost complaints.

## Context — "a migrate may fail," and a hard retire leaves debris

`device_push` moves a device to another network (repoint → confirm on the target → **retire on the old
box**). The retire called `device_migrate.run_migration("retire", …)`, whose own docstring is explicit:
it deletes the device's **data** (`hot.db` `readings`/`device_last_seen`/`summaries`, parquet, rungs,
`control.db`) but **"NOT the registry (`devices.yaml`/`tasmota-devices.yaml`) — config decisions."**

Two failure modes fell out of that seam when the failover PMs migrated (2026-07-08):
1. **Partial retire.** The retire cleared `device_last_seen` but the `readings` survived. Because the
   sensor view `LEFT JOIN`s `device_last_seen`, **orphaned readings alone re-surface a device** — with a
   stale timestamp, tripping the "unreachable" alert and spamming the GUI/ntfy.
2. **Lingering registration.** The `tasmota-devices.yaml` entry and the retained MQTT LWT remained, so the
   bridge kept the device in its subscription set — a ghost that outlives its data cleanup.

A hard, immediate retire is also **irreversible and brittle**: if the migration only *looked* successful
(the device is actually still on the old network), deleting it is data loss and a live device vanishing.
The right model treats "this device left" as a **hypothesis to be confirmed over time**, not an instant
fact — and must not fight the [[data-storage-is-primary]] directive.

## Decision — hold migrated devices *pending*, then drop with self-heal

Introduce a **pending-hold** lifecycle state, riding the existing R8 device-meta overlay
(`device_meta.hidden`/`retired`; ADR-0014):

- **`device_meta.pending_until` (ISO instant).** Setting it holds the device **QUIET immediately** — it's
  dropped from the sensor list, which is *also* the source alerts derive from, so **no GUI + no ntfy** the
  moment it's set. (This is the property the user emphasized.)
- **`device_push` sets a pending-hold (default 6 h) instead of a hard retire** after the target confirms.
  No data is deleted; the device just goes silent on the old box.
- **`ha-pending-sweeper` (systemd timer, 15 min) resolves each held device:**
  - **reported fresh data within the window** → **clear the hold** — a *failed* migration **self-heals**
    (the device is alive and reappears) instead of being deleted;
  - **window expired, still silent** → **drop = mark `retired`** (hidden, but **history kept** — never a
    `DELETE`, per [[data-storage-is-primary]]) + clear pending;
  - **still holding, still quiet** → leave it.

So "left the network" is *confirmed by absence over the grace window*, and the terminal state reuses the
existing `retired` semantics ("archived, not expected to report again — history kept"). The GUI/ntfy
suppression is immediate; the irreversible-ish `retired` transition waits for evidence.

## Consequences

- **No more stale ghosts.** A migrated device is invisible + silent from the instant it's marked, and is
  never a screaming "unreachable" alert during or after migration.
- **Failure-tolerant.** A migration that didn't actually take is *self-correcting* — the device's own
  fresh data cancels the hold. Retirement is never a one-way delete triggered by an unconfirmed guess.
- **History is preserved.** Drop = `retired`, not deletion; the migrated device's old-box history remains
  queryable (and its authoritative copy lives on the target box anyway).
- **Small surface.** One nullable column + one sweeper + a one-line `device_push` change + a viewmodel
  exclusion, all on the tested R8 overlay path. Pure `sweep()` + accessors are unit-tested.
- **Still-open seam (follow-up):** the pending drop marks `retired` (which hides it) but does **not** yet
  remove the file-registry entry (`tasmota-devices.yaml`) or clear the retained LWT — harmless to the GUI
  (retired hides it) but untidy on the broker/registry. Fold registry-deregistration into `device_push`
  at migration time, or into the sweeper's drop. (For the 2026-07-08 PMs this was done by hand.)

## Rejected alternatives

- **Fix retire to also delete the registry + LWT (a "complete" hard retire).** Still an instant,
  irreversible delete on an *unconfirmed* migration — the failure mode (device still alive → vanishes /
  data lost) remains, and it violates data-storage-is-primary. Pending-hold is confirm-by-absence.
- **Just hide the device (`hidden`) on migrate.** Hides the GUI symptom but never resolves — the ghost
  lingers forever, and a genuinely-gone device is never archived. Pending-hold auto-terminates.
- **Immediate `retired` on migrate.** No grace window, so a failed migration can't self-heal, and a
  transient confirm blip prematurely archives a live device.

## Implementation (as built — 2026-07-08, dev)

- `server/control/control_store.py` — `device_meta.pending_until` (additive migration) + `set_pending` /
  `clear_pending` + accessors carry it.
- `server/api/viewmodel.py` — `_pending_active()`; `build_sensor_list` drops held devices (⇒ out of the
  alert set too).
- `server/maintenance/pending_sweeper.py` + `systemd/ha-pending-sweeper.{service,timer}` — the resolver.
- `server/maintenance/device_push.py` — step 5 sets a `PENDING_HOURS`-hour hold, not a hard retire.
- `tests/test_pending_hold.py` — accessors, exclusion, and sweep(fresh/expired/hold). 5/5.
