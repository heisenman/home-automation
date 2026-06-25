# ADR-0018 — Node provisioning & elevation to record-keeping status

**Date:** 2026-06-25
**Status:** **IMPLEMENTED & LIVE (2026-06-25)** — `failover/reconcile-parquet.sh` (row-level deep-reconcile,
zstd/6/100k + sort to match the compactor), `failover/provision-peer.sh` (config→hot→archive→HARD GATE), and
the `cluster-doctor` "Archive completeness" gate are shipped + deployed on 210/.245. **Proven by the one-off:**
210 elevated to record-keeping from `.245` — both boxes converged to **8,777,107 rows, earliest 2026-01-07**,
gate PASS, `cluster-doctor` 18 pass / 0 warn / 0 FAIL. The bidirectional union recovered 26,128 June rows
unique to 210 (rsync would have dropped them). **Deferred follow-on:** `parquet-manifest-wiring` (dev) —
auto-rebuild the ADR-0004 hash manifest after a reconcile on BOTH boxes (the push rewrites the peer's
partitions) + a `cluster-doctor` manifest-consistency assertion; and the optional VIP-gated `--loop` ongoing
service. Promotes the parquet **deep-reconcile** that [ADR-0016](ADR-0016-failover-history-reconciliation.md)
deferred into a load-bearing, gated mechanism. Builds on [ADR-0007](ADR-0007-device-history-sync.md) (idempotent
ingestion — the merge key), [ADR-0006](ADR-0006-storage-two-tier-sqlite-parquet.md) (hot/parquet tiers),
the `failover/` control plane (`sync-standby.sh`, `reconcile-history.sh`, `cluster-doctor.sh`,
`dictator-files.manifest`), and the 2026-06-24 cutover-incident hardening that produced the manifest.

## Context
The dictator↔standby cluster had a procedure for *running* and *failing over*, but **no defined procedure
for bringing a brand-new box up as a peer and elevating it to a trustworthy record-keeper.** A new box got:
live ingestion (it publishes/subscribes once it has the broker + config), config-of-record (`sync-standby`
replicates the manifest secrets + `control.db`/`mesh.db`), and the hot-tier divergence window
(`reconcile-history`). What it did **not** get was the **historical parquet archive** — months of compacted
readings that live only in each box's `instance/db/parquet/`.

This surfaced on **2026-06-25** via a user-visible symptom (dashboard graphs flat across every time range).
Root cause: **210 was elevated to dictator (~06-24) holding only ~1.5 d of archive**, while **`.245` held
the full archive since 2026-01-07 (8.75M rows, 13 devices, 57 MB)**. The cutover seeded hot+config but never
the archive. No data was lost — it was safe on `.245` — but the *dictator of record* served a truncated
timeline, and nothing in the system flagged it. ADR-0016 had explicitly deferred the parquet deep-reconcile
as "not load-bearing until an outage exceeds min device-buffer"; this incident shows it is load-bearing the
moment a **fresh** box is elevated (its archive gap is the *entire* history, not a swap-window slice).

The deeper lesson: **"record-keeping status" must be a first-class provisioning stage with a HARD gate**,
not an emergent side effect of having run for a while.

## Decision
**1. Define three distinct node statuses** (a box advances through them; they are not the same thing):
   - **can-ingest** — has broker + config; readings land in its hot tier.
   - **VIP-eligible** — keepalived-capable; can hold the floating VIP and *control* (actuate). Control
     correctness already gated by `dictator-files.manifest` completeness (ADR-0011 / cutover hardening).
   - **record-keeping (dictator-of-record)** — additionally holds the **full data-of-record**: hot tier
     *and* the complete parquet archive, convergent with the cluster's deepest copy. **NEW, and gated.**

   Control failover does **not** wait on record-keeping (a promoted box actuates immediately on config; live
   safety never blocks on history). But a box is not *trusted as the archive of record* until it passes the
   gate — and `cluster-doctor` warns whenever the VIP sits on a box that hasn't.

**2. Provisioning is a scripted, idempotent, gated procedure** — `failover/provision-peer.sh --from <src>`:
   1. **config-of-record** — `sync-standby.sh` (manifest `sync` files + `control.db`/`mesh.db` snapshots).
   2. **hot tier** — `reconcile-history.sh --once`.
   3. **archive** — `reconcile-parquet.sh --once` (below).
   4. **HARD GATE** — archive-parity assertion vs the source: this box's archive must be no shallower and
      no materially smaller. Pass ⇒ *record-keeping eligible*; fail ⇒ explicitly **not** trusted.
   `--data-only` skips step 1 for re-provisioning a box whose **config is already authoritative** and only
   its **data** is thin (exactly the 210 one-off below).

**3. Archive reconcile is ROW-LEVEL, not file rsync** (Hugh's call) — `failover/reconcile-parquet.sh`.
   Per monthly partition, the merged result is the **DISTINCT union** of both boxes' rows keyed on the
   writer's identity **(device_id, ts, metric)** — the same idempotency contract as hot-tier ingestion
   (ADR-0007). Each partition is rebuilt (DuckDB `read_parquet(union_by_name)` → dedup → `COPY`) to a temp
   file and **atomically `mv`'d** into place. Bidirectional, over the `id_cluster` SSH back-channel only.
   *Why row-level over rsync:* two boxes can each hold different rows for the **same** month (each ingested
   during its own reign); a file-level rsync can't merge them without picking a loser, while a keyed union
   is lossless and self-correcting. Cost is trivial (archive is tens of MB; re-merge of identical sets is a
   no-op), so an ongoing **VIP-gated slow loop** (`--loop`, ~6 h; parquet only changes on daily compaction)
   keeps archives convergent without the per-reading replication ADR-0016 rejected.

**4. The gate is enforced continuously**, not just at provisioning — `cluster-doctor.sh` gains an
   **"Archive completeness (ADR-0018)"** check: both dictator-capable boxes' parquet archives must converge
   (row count within ~1% + floor, and neither materially shallower). Divergence is a **FAIL** ("a failover
   to the thin box now serves truncated history"). This is the durable backstop that makes the 2026-06-25
   silent-truncation impossible to repeat.

## The one-off (this incident)
Run the *new* procedure against the present dictator instead of a throwaway scp — dogfooding the mechanism
on the box that exposed the gap, **omitting the demote/promote step** (210 is already the live dictator and
its config is authoritative):

```
# on 210, the present dictator; .245 holds the deep archive
failover/provision-peer.sh --from 192.168.0.245 --data-only --yes
```

This pulls + row-merges `.245`'s archive into 210 (and pushes 210's superset 06-24 back — union is
lossless both ways), then asserts the gate. Expected post-state: 210 earliest reading `2026-01-07…`,
`cluster-doctor` archive check PASS, and the dashboard's 7d/30d ranges show real history.

## Consequences
- **+** A new box (or a recovered/rebuilt one) reaches trustworthy record-keeping by **one idempotent
  command** with a pass/fail verdict — no tribal-knowledge sneakernet, no silent truncation.
- **+** Reuses every proven part (manifest, `sync-standby`, `reconcile-history`, the `id_cluster` channel,
  ADR-0007 idempotency). The archive reconcile is the only genuinely new transport, and it mirrors the
  hot-tier one.
- **+** `cluster-doctor` now asserts the *full* data-of-record, closing the last "looks healthy but isn't"
  gap (config ✓ + hot ✓ + **archive ✓**).
- **−** Provisioning a truly fresh box transfers the whole archive once (tens of MB today; grows with
  history) — bounded, one-time, over LAN SSH; negligible.
- **−** The row-level partition rewrite holds a partition in DuckDB memory to merge it; monthly partitions
  keep that bounded. Atomic `mv` means a query either sees the old or new file, never a partial.
- **−** The gate can *block* trusting a box as dictator-of-record; that is the point. Control/VIP failover is
  deliberately **not** blocked (live safety first) — only the *record-of-truth* trust is gated.

## Rejected alternatives
- **File-level rsync of the parquet tree** — simplest, but cannot merge two boxes that each hold distinct
  rows for the same month without choosing a loser; lossy across real bidirectional divergence.
- **Leave the deep-reconcile deferred** (ADR-0016 status quo) — the incident proves it is load-bearing at
  elevation time, not just after a long outage.
- **Block control failover on archive completeness** — wrong trade: live actuation must never wait on
  history. Gate *record-keeping trust*, not the VIP.
- **Treat "ran for a while" as record-keeping** — exactly the implicit assumption that failed on 2026-06-25.
