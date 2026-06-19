# ADR-0006 — Two-Tier Storage: SQLite Hot + Parquet Cold

**Date:** 2026-06-19  
**Status:** Accepted

## Decision

All raw readings land in a single WAL-mode SQLite file (hot tier, ~current day/week).
A daily compaction pass flushes to partitioned Parquet (Zstd, long format, Hive-style
`year=/month=/`) and prunes SQLite. DuckDB queries both tiers in a single SQL statement.

## Context

Time-series databases add operational complexity. Small Parquet files (per-reading/minute)
create inode and small-file amplification problems. SQLite as a batch-staging area before
Parquet provides a clean write path with durable, queryable in-progress data.

## Consequences

- One writer process only (no multi-writer SQLite contention)
- WAL mode: readers never block the writer; writer never blocks readers
- Long format (`ts, device_id, metric, value, unit, area, transport`) — columnar Parquet
  with Zstd achieves ~10–30× compression; years of multi-sensor data fits in hundreds of MB
- Summary tier computed in the same compaction pass (one read → two outputs)
- DuckDB: embedded, no server; queries `hot.db` + Parquet glob in one SQL statement
- Flush cadence (daily vs monthly) is the single tuning knob; start daily

## Rejected alternatives

- InfluxDB/TimescaleDB: additional service, schema migration complexity, harder to "keep
  everything" with custom retention policies
- Per-reading Parquet: small-file/inode blowup; batching via SQLite is the fix
- Single SQLite forever: will grow without bound; Parquet+Zstd is the compression path
