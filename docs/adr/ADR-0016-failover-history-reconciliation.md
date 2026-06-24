# ADR-0016 — Sensor-history reconciliation across a dictator failover

**Date:** 2026-06-24
**Status:** Proposed — spun out of [ADR-0015](ADR-0015-edge-relay-coverage-assignment.md) decision #8
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
   - export its own `readings` for the divergence window (`ts >= now - WINDOW`, default a few days ≫ the
     longest plausible outage) into a throwaway sqlite snapshot (`sqlite3 .backup` or a `SELECT … INTO`),
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
