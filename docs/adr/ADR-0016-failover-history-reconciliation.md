# ADR-0016 — Sensor-history reconciliation across a dictator failover

**Date:** 2026-06-24
**Status:** Accepted (2026-06-24, Hugh) — spun out of [ADR-0015](ADR-0015-edge-relay-coverage-assignment.md) decision #8
("history reconciliation → its own ADR; reconcile-on-promotion over the cluster back-channel"). Builds on
[ADR-0007](ADR-0007-device-history-sync.md) (idempotent ingestion), [ADR-0009](ADR-0009-history-continuity.md)
(continuity), [ADR-0006](ADR-0006-storage-two-tier-sqlite-parquet.md) (hot.db/parquet), and the failover
control plane (`failover/`).

## Context
Failover moves the floating VIP — and therefore *which box ingests live data* — between the primary (210)
and the standby (.245). **Only the dictator ingests:** edge nodes + the local scanner publish to the VIP
holder's broker, so the standby's `readings` DB is frozen during the other's reign. The reigns are disjoint,
so after a fail-over/fail-back cycle **each box's history has a hole for the other's reign** — and over
repeated swaps both diverge. The *control* plane already fails over cleanly (ADR-0011) and the dashboard
survives (ADR-0015 #9, ha-api warm/mount-on-promote); this ADR closes the remaining **data**-plane gap.

What makes it tractable: **ingestion is idempotent** (ADR-0007 — `UNIQUE(device_id, ts, metric)` +
`INSERT OR IGNORE` on every write path). So merging the peer's rows can never duplicate; an overlapping
re-merge is a no-op. A cross-cutting rule from ADR-0015 (Hugh): history syncs over the **cluster
back-channel** (SSH / `/cluster` RPC), **never** the device bus (`home/#`) or GitHub.

## Decision
**Reconcile-on-promotion, bidirectional, over the cluster back-channel — not continuous replication.**
A bounded gap during the rare swap window is acceptable for sensor history; continuous time-series
replication would pay a constant cost to close a gap that only opens on a failover.

1. **Trigger.** On every VRRP transition, `notify.sh` fires a best-effort, async `failover/reconcile-history.sh`
   (like the existing `sync-standby` / mapper-restart hooks) — it **never blocks the takeover**. Promotion
   and demotion both reconcile; idempotency makes "reconcile on both ends" safe and self-correcting.
2. **Mechanism (bidirectional snapshot-merge).** Each box, against its peer over `ssh -i ~/.ssh/id_cluster`:
   - export its own `readings` for the divergence window (`ts >= now - WINDOW`, where **WINDOW tracks the
     hot-tier compaction horizon** — the daily compactor's cutoff, not a hand-tuned constant — so it self-tunes
     and targets exactly the zone where live divergence lives) into a throwaway sqlite snapshot
     (`sqlite3 .backup` or a `SELECT … INTO`),
   - `scp` it to the peer, which does `ATTACH` + `INSERT OR IGNORE INTO readings SELECT … FROM snap`.
   - Run it **both directions** so each box backfills exactly the rows it missed during the other's reign.
   No timestamps-of-last-sync bookkeeping needed — the unique key is the merge contract.
3. **Scope = the hot tier.** `hot.db` (recent, pre-compaction) is where live divergence lives, so the window
   targets it. Parquet (compacted, older) only diverges if a swap straddles a compaction boundary — handled
   by a wider periodic deep-reconcile (deferred; rare, and the hot-tier merge covers the common case).
4. **Idempotent + observable.** Log rows-merged per direction to `/var/log/ha-failover.log`; a follow-up
   `cluster-doctor` check can assert post-reconcile that the two boxes' recent row-counts per device converge.

## Consequences
- **+** Both boxes converge to the *full* timeline after any swap — no permanent holes. Cheap: work happens
  only on transitions, proportional to the gap, and the unique key makes it self-healing if a reconcile is
  missed (the next transition catches up).
- **+** Reuses proven parts: ADR-0007 idempotency, the `id_cluster` back-channel (`sync-standby`), the
  `notify.sh` hook pattern. No new always-on service.
- **−** A brief window right after promotion where the new dictator's history isn't yet backfilled (until the
  async reconcile completes) — acceptable for *history* (not live control). **−** A very long outage could
  exceed the hot-tier `WINDOW`; mitigated by sizing WINDOW generously + the deferred parquet deep-reconcile.
- **−** Reconcile reads the peer's DB over SSH — bounded by WINDOW so the transfer stays small.

## Retention horizons & why the parquet deep-reconcile is safely deferred
A gap is recoverable from **either** a peer box's parquet **or** the source device's own history buffer
([ADR-0009](ADR-0009-history-continuity.md) relay-primary/buffer-pull; [ADR-0007](ADR-0007-device-history-sync.md)).
Three nested horizons bound recovery:
1. **Hot tier (~1–2 d, the compaction horizon)** — the cheap sqlite cross-box merge above; `WINDOW` tracks this.
2. **Parquet (kept per-box, but diverged)** — the deferred periodic deep-reconcile.
3. **Device ring buffer** (SwitchBot ≈68 d, *wrap-limited* per ADR-0007; Aranet = its own onboard-log depth,
   TBD/measure) — source-of-last-resort. Once a meter's *circular* buffer **wraps**, the oldest data is
   overwritten **and** the read protocol changes (the `02`-NAK that already blocks attic/h_bed, ADR-0009) — so
   the net is finite *and* weaker than nominal for already-wrapped meters.

Permanent loss requires a gap older than what the boxes will cheaply share **and** past the device buffer/wrap.
The device buffer is therefore the explicit **deferral bound**: the parquet deep-reconcile is not load-bearing
until an outage exceeds `min(device-buffer)`. That makes it a *deadline*, not a someday — a `cluster-doctor`
assertion should warn when **peer-frozen-duration > min device-buffer depth** (the device-pull net has expired
for the oldest slice of the gap → cross-box parquet reconcile must run *before* the device wraps).

- **Near-term (small) lift:** record a per-device-**model** buffer-depth attribute in the registry (a documented
  constant — *not* a per-device retention-policy engine, which stays out of scope) + add the `cluster-doctor`
  deadline assertion above. This converts "device-pull will save us" from an implicit assumption into a stated,
  monitored bound.
- **Deferred (heavier) lift:** the parquet deep-reconcile transport itself, landed alongside the full
  dictator-handover reconcile implementation (`adr-history-reconciliation`).

## Rejected alternatives
- **Continuous time-series replication** (stream every reading to the standby): constant cost + complexity to
  close a gap that only opens on a rare failover; and the standby's broker doesn't even carry the adverts.
- **Transport over `home/#` or GitHub:** violates the cluster-back-channel rule (couples the data bus / leaks
  history to a public remote). SSH/`/cluster` only.
- **Trust-the-clock incremental sync** (only rows since last-sync ts): fragile across clock skew/missed runs;
  the idempotent full-window merge is simpler and self-correcting.

## Implementation sketch (for the follow-up task)
`failover/reconcile-history.sh` (bash, `id_cluster`, mirrors `sync-standby.sh` structure) + a `notify.sh`
hook on MASTER/BACKUP (best-effort, async) + a `cluster-doctor` convergence check. Gated/manual first run,
validated on `.245` against a synthetic gap before wiring into the live transition path.
