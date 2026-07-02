# ADR-0022 — Multi-Resolution Rollup Ladder (minute / hour / day / week)

**Date:** 2026-07-02
**Status:** Accepted (Hugh, 2026-07-02)

Extends [ADR-0006](ADR-0006-storage-two-tier-sqlite-parquet.md) (two-tier sqlite/parquet — which already
named a "summary tier"), feeds [ADR-0016](ADR-0016-failover-history-reconciliation.md)/[ADR-0018](ADR-0018-node-provisioning-record-keeping.md)
(reconciliation), and unblocks [ADR-0019](ADR-0019-screen-interface-architecture.md) panels as local
data-backup + graphing nodes. Built as modules per [[feedback-modularize-new-architecture]] (ADR-0020 spirit).

## Context

The summary tier from ADR-0006 is only **half-built**: the daily compactor computes a **daily** `summaries`
row (`min/max/mean/median/count/last` per `date,device_id,metric`) and the BFF serves it as a min/avg/max
band (`/devices/{id}/summary`). But there is **nothing between raw and daily** — a cliff. A week/month chart
must either haul raw or jump to coarse daily.

Measured reality (2026-07-02, `.210`): **~133 k readings/day** across ~32 series (13 devices × ~2.5 metrics)
= **~21 s per series** raw cadence, **~48 M rows/year**. As storage that is **~2 GB/yr parquet** vs
**~12–14 GB/yr sqlite** (~6–7×; sqlite is penalised by TEXT timestamps + the `UNIQUE` index). Two hard
constraints fall out: parquet is **unreadable on the ESP32-P4** (no Arrow/DuckDB for IDF), and FAT32 (the
panel's filesystem) caps a **single file at 4 GB** — so a monolithic sqlite archive would hit the wall in
months and must be sharded regardless.

The unlock is a property of **graphing**, not storage: a chart N pixels wide cannot display more than ~N
points; beyond that it overplots. So at any zoom, a **min/max/mean rollup at the right resolution is visually
identical to raw**. The rollup ladder is therefore both the storage-bound *and* the graph-fidelity solution.

## Decision

A **multi-resolution rollup ladder** — `{1min, 1hour, 1day, 1week}` — is the system's canonical long-history
representation. Raw is demoted to a short recent window; graphs and the panel replica ride the ladder.

1. **Rungs + retention** (bounded storage forever):

   | Rung | Retention | Notes |
   |------|-----------|-------|
   | raw | short window (days) | hot.db + parquet archive (recovery/forensics); not the graph source past the window |
   | 1 min | ~90 days | finest rung (raw is ~21 s/series, so 10 s would be finer than the data — **rejected as redundant**) |
   | 1 hour | ~2 years | |
   | 1 day | forever | (the existing daily `summaries`, generalised) |
   | 1 week | forever | month/quarter rungs deferred until data volume makes them relevant |

2. **Per-bucket aggregates: `min, max, mean, count, last`.** `min`+`max` are mandatory — they preserve the
   visual envelope so a transient in one raw sample survives downsampling (mean-only silently erases spikes).
   `median` is kept only at the daily-from-raw level (it is **not** hierarchically composable).

3. **Hierarchical, incremental composition.** Each rung is computed from the rung **below**, never by
   rescanning raw: `min=min(mins)`, `max=max(maxes)`, `count=Σcount`, `mean=Σ(mean·count)/Σcount`, `last=last`.
   1hour←60×1min, 1day←24×1hour, 1week←7×1day. Cheap and continuous.

4. **Resolution-selection at query time (visually lossless).** `/readings` (server) and the panel graph
   source pick the rung so a chart returns ~≤1000 points for its span (≤2 h → raw/1min, ≤2 mo → 1hour, years
   → 1day, decades → 1week). Return `min/max/mean` per bucket → band + line, pixel-identical to raw.

5. **Format follows function, and the debate is retired.** Raw stays **parquet** on the server (dense,
   immutable, per-day-sharded → fits FAT32, recovery/forensics). The **rungs are compact sqlite** (MB, not GB;
   portable; readable on the server, the standby, *and* the ESP32). So the panel replicates and reads the
   **rung sqlite** — never parquet, never a 12 GB/yr monolith. This is the "direct readable copy = coherent"
   property Hugh asked for.

6. **Module boundaries** (module-first):
   - **Server rollup engine** (dev) — promote the compactor's daily summary step into an incremental ladder
     engine: advance 1min continuously, cascade up, enforce per-rung retention. Its own module + timer.
   - **Query resolution-selector** (dev/server) — `/readings` picks the rung by span.
   - **`ha_replica`** (ops/firmware) — replicate the rung DB (+ the parquet recovery archive + the opaque
     encrypted config blob) to the panel SD; a panel graph-source reads the local rung sqlite. Map updated on
     both ends (`edge/MATRIX.md`, `server/AGENTS.md`).

## Consequences

- **Storage is bounded forever** — long history lives in the rungs (MB); raw is a short window. The
  ~12 GB/yr sqlite and 4 GB FAT32-file problems both vanish; parquet's density stops being load-bearing for
  *graphing* (only for the short raw archive).
- **Panel gets full-range offline graphing** from local SD, at every zoom, visually identical to raw —
  ADR-0019's app-capability upgrade, delivered.
- **Coherence** — rung sqlite is a direct, readable file copy server → standby → panel; it reconciles the same
  idempotent way as `readings` (ADR-0016), keyed by `(res, bucket_start, device_id, metric)`.
- Rungs are **derived** — losing them is never data loss while raw/parquet survive; they rebuild from below.

## Rejected alternatives

- **10 s rung** — finer than the ~21 s/series raw cadence; can't summarise finer than you sample. Redundant.
- **Month/quarter rungs now** — premature; 1 day/1 week cover decades at this volume. Add when relevant.
- **mean-only buckets** — erase spikes; the min/max band is the whole point of visual losslessness.
- **sqlite-for-everything (raw included)** — ~12–14 GB/yr + the 4 GB FAT32 single-file wall in months.
- **parquet on the panel** — no Arrow/DuckDB on ESP-IDF; heavy; defeats "readable copy."
- **Keep only the daily tier** — the raw→daily cliff is exactly the gap that forces hauling raw or accepting
  coarse charts for week/month spans.

## Open follow-ups

- Exact retention constants are a tuning knob (start with the table above); revisit like the ADR-0016 shadow
  tuner if a rung grows unexpectedly.
- Whether raw is pruned entirely after summarisation or kept as parquet for forensic replay — **kept** for now.
- Sample-rate policy (whether ~21 s/series is more resolution than wanted) is **orthogonal** to this ADR and
  can be decided separately; the ladder makes either rate work.
