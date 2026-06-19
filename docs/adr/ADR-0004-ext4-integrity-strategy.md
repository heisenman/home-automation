# ADR-0004 — EXT4 Integrity Strategy (Application-Level Hash Manifest)

**Date:** 2026-06-19  
**Status:** Accepted

## Decision

Since the server runs EXT4 (no data-block checksums), bit-rot defense moves to the
application layer: a SHA-256 hash manifest of all immutable Parquet partitions is
maintained and verified on a schedule and before any restore.

## Context

ZFS/btrfs provide block-level checksums and scrubbing. EXT4 does not. Silent data
corruption in cold-tier Parquet files would be undetectable without an alternative.

## Consequences

- Compactor writes `data/parquet/manifest.json` after each partition flush (SHA-256 +
  file size + row count)
- `tools/verify_hashes.py` checks every partition against the manifest; runs weekly
  via systemd timer and before any restore
- Parquet's internal page CRCs provide intra-file detection; the manifest covers
  file-level replacement/truncation
- Multiple copies (replication + off-site) provide redundancy

## Rejected alternatives

- ZFS/btrfs: requires reformatting the current dev server; deferred to target hardware
- Trust EXT4 + backups only: risk of undetected corruption propagating into backups
  before discovery
