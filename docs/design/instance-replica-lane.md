# Instance-replica lane — D1001 roadmap #7 "deepen recovery" (ability H)

**Status:** design/contract (decompose-before-dev). Extends ADR-0022 (rollup ladder + panel replica) and
`rollup-ladder-and-replica-sync.md`. Cross-agent: **server half = dev/`.210`**, **panel half = ops/bench**.

## Goal
Today `ha_replica` mirrors only the **rung ladder** (`rungs.db`) to the panel's SD so charts render offline
(ADR-0022). #7 deepens the panel into a fuller **warm-standby backup of the dictator's `instance/`** — the
data-of-record: **config** (the gitignored `instance/*.yaml` etc.), **parquet** (the cold history day-files),
and **`hot.db`** (the live readings DB). If the dictator is lost, the panel holds a recent, provenance-tagged
copy of everything needed to reconstruct state, not just the rollups.

**Non-goals:** the panel does not *serve* or *reconcile* this data (it is a cold backup, read-nowhere until a
human restores it); no live query path over the replica. This is explicitly the roadmap's lowest-value,
"diminishing returns, sequence last" item — keep the mechanism simple (whole-file-by-hash), not a bespoke
delta protocol.

## Artifacts & cadence
| artifact | source | change rate | sync unit |
|---|---|---|---|
| config | `instance/*.yaml`, `control.yaml`, `areas.yaml`, `devices.yaml`, … | rare | whole file, by sha256 |
| parquet | `instance/parquet/**/*.parquet` (ADR-0018 cold history) | append-mostly, new day-files | whole file, by name+sha256 (immutable once sealed) |
| hot.db | `instance/db/hot.db` | continuous | whole file, by sha256 (snapshot copy) |

Cadence: config + parquet check hourly (slow-moving); `hot.db` snapshot a few times/day (it's the biggest and
always-changing — a periodic cold copy is the backup, not a live mirror). All well below the rung lane's 10-min.

## Server contract (dev / `.210` — new routes on the existing BFF, mirrors the `/rung/*` shape)
- `GET /api/v1/replica/manifest.json` → the backup set the panel diffs against:
  ```json
  { "source_tag": "dictator@g11", "is_source_of_record": true, "generated_ts": "…Z",
    "artifacts": [ {"kind":"config","name":"control.yaml","sha256":"…","size":123,"mtime":"…Z"},
                   {"kind":"parquet","name":"2026/07/2026-07-04.parquet","sha256":"…","size":…},
                   {"kind":"hotdb","name":"hot.db","sha256":"…","size":…} ] }
  ```
  - **source_tag** = which box authored this (hostname/role) — provenance the panel records so a restore knows
    where the bytes came from.
  - **is_source_of_record / ADR-0018 gate:** the manifest is only "trustworthy for backup" when served by the
    current **source-of-record** box (the reconcile/failover winner, per ADR-0016/0018). The endpoint sets
    `is_source_of_record` from the SAME signal the reconcile job uses (dev wires this — it owns the
    dictator-role mechanism). When false (this box is a standby), the panel MUST NOT overwrite its good replica
    from it — it keeps the last source-of-record copy. This prevents a demoted box from poisoning the backup.
- `GET /api/v1/replica/file?kind=<config|parquet|hotdb>&name=<name>` → `FileResponse` of one artifact,
  path-validated to `instance/` (no traversal). `hot.db` served as a consistent snapshot (sqlite backup API or
  copy-under-read-lock), not a live-mutating file.

## Panel contract (ops / bench — a "files lane" in `ha_replica`)
Extend `ha_replica` with a second lane alongside the rung sync (same task, lower cadence):
1. `GET /replica/manifest.json`. If `is_source_of_record` is false → skip this cycle (keep the good copy).
2. Diff each artifact's sha256 vs the local `/sdcard/replica/<kind>/<name>`. Download changed/missing via
   `/replica/file`. Parquet day-files are immutable once sealed → only ever fetched once.
3. Write atomically (temp + rename) under `/sdcard/replica/`. Record `source_tag` + `generated_ts` in a small
   `/sdcard/replica/PROVENANCE.json` so a restore knows what it holds and from where/when.
4. SD budget: cap total replica size; prune oldest parquet day-files first if over budget (config + hot.db are
   always kept). Log what was dropped (no silent truncation).

## Split & sequencing
1. **Contract (this doc)** — done.
2. **Server (dev, board `instance-replica-server`):** the two routes + the source-of-record gate wiring +
   the hot.db snapshot. Production BFF change on the live dictator → dev's domain.
3. **Panel (ops, bench):** the `ha_replica` files lane, built against the contract; unit-testable offline with
   a stub manifest, end-to-end validated once dev's routes are live.
4. **Validate:** panel pulls config+parquet+hot.db to SD; provenance recorded; demote the box (or stub
   `is_source_of_record:false`) → panel refuses to overwrite; SD-budget prune logged.

## Why simple-by-hash (not a delta protocol)
Config/parquet/hot.db are whole files; parquet day-files are immutable once sealed and config rarely changes, so
a sha256 manifest-diff transfers only what actually changed with near-zero complexity. A row-level delta (the
`/rung/since` NDJSON style) buys nothing here and is the "diminishing returns" the roadmap warned about — skip it.
