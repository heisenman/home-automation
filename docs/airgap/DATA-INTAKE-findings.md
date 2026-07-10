# Data-intake findings — dev system → ha-2 (sandbox-validated, 2026-07-09/10)

Companion to [DATA-INTAKE-inventory.md](DATA-INTAKE-inventory.md) (the plan) and ADR-0031 (the decision).
Records what the **sandbox-first** convergence actually found, so the project learns from the process.
All figures anonymized (no MACs/tokens; logical `device_id`s are non-secret; MAC-derived ids shown as
`unknown_<mac>`).

## Method that worked

We control every box and the real data is tiny (~60M live, vs a 12G `backups/` red herring), so: **clean
sqlite `.backup` snapshots + an rsync'd parquet copy into a scratch dir → run every merge on the copies →
verify → and only then copy verified results back.** No synthesis touched the live ha-2 dictator during
design. This is the pattern to reuse for any future cross-box data convergence.

## Proven per-class results (all merges run on copies)

| Class | Method | ha-2 before | after / merged | Δ to ha-2 | Verdict |
|---|---|---|---|---|---|
| parquet archive | row DISTINCT-union `(device_id,ts,metric)` | 10,623,981 | 10,811,676 | **+187,695** | additive, 23 devices |
| ↳ stranded gas back-history | (in the union) | 0 | 30,658 | — | `gas_h_office` 10,369 · `gas_hbed` 14,534 · `gas_standby` 5,755 |
| hot.db readings | `INSERT OR IGNORE` (unique index) | 209,622 | 327,062 | **+117,440** | additive; current-window divergence is large |
| control_log | full-row DISTINCT union (no PK/id) | 45,334 | 52,342 | **+7,008** | additive; recovers pre-migration actuations |
| weather.db | **transfer** (ha-2 had none) | 0 | 22,524 | +22,524 | 6 mo (Jan–Jul), 4 metrics |
| rungs + summaries | **rebuild** via `rollup.py` from merged archive | absent / 1 day | (regenerated) | — | derived — never transferred |
| device_meta | **review → skip** (see below) | — | — | 0 | no real data lost |
| mesh.db | low-priority topology | — | — | defer | — |

## Gotchas the project should learn from

1. **`hot.db` is only the rolling ~1-day window** — the deep history is in `parquet`. Don't read a hot-tier
   row count as "all the data." (This is why the 12G scare was a non-event.)
2. **ha-2 was ~99% seeded at migration.** The real stranded data was small: 3 gas nodes' back-history +
   ~187k archive rows + weather + some control history. The divergence is **mutual** (each box holds rows the
   other lacks) → **union, never one-way copy**.
3. **The reconcile tooling covers the sensor tier only** (`reconcile-history` hot, `reconcile-parquet`
   archive). `rungs`, `summaries`, `weather`, `control_log`, `device_meta` had **no merge path** — each
   needed a bespoke, verified strategy.
4. **`reconcile-parquet`'s "rebuild" is the hash *manifest* only, not `rungs`.** Derived tiers (`rungs`,
   `summaries`) are rebuilt by `server/storage/rollup.py` — rebuild them on the target *after* the archive
   merge, don't try to transfer them.
5. **`control_log` has no primary key / id** — merge by **full-row DISTINCT union**. (Trade-off: two
   genuinely-identical rows at the same second collapse to one — acceptable for an idempotent actuation log.)
6. **Snapshot-overwrite would have corrupted state — exactly as feared.** `device_meta` carries mutable
   state (`retired`, `hidden`) that **diverged**: dev has `dehumidifier_living_room` marked **`retired=1`**
   (stale, from its dev-side pending-hold) while that device is **live on ha-2**. A naive snapshot/merge
   would have wrongly retired a live production device. **Never clobber the dictator's live truth with the
   dev system's stale state.**
7. **Dev cruft had already leaked into production:** `ui_smoke_dev` (a UI smoke-test pseudo-device) exists
   in ha-2's `device_meta`. It should be **purged from ha-2** — and `device_meta` intake is otherwise
   **unnecessary** (ha-2 already holds the only non-empty names; the dev-only rows are empty-name + stale
   flags). So the right move is *skip the merge, clean the cruft*.
8. **`cluster.env` pins `PEER_HOST` to the household standby (`.245`)** — the default bidirectional reconcile
   would sync the wrong pair. A dev→ha-2 intake must target ha-2 explicitly (or use the low-level primitives).

## Copy-back (the single verified prod write — reversible)

Order, each step verified against the sandbox result first, ha-2's current state backed up as a rollback
point before the write:
1. Back up ha-2 `parquet/` + `.backup` its `hot.db`/`control.db`; place a `weather.db` copy.
2. parquet: atomic per-partition row-union swap-in → **+187,695** rows, 23 devices.
3. hot readings: `INSERT OR IGNORE` union → **+117,440**.
4. control_log: full-row union into a rebuilt table → **+7,008**.
5. weather.db: place (ha-2 had none) → **22,524** rows.
6. `rollup.py` full rebuild → regenerate `rungs` + `summaries` from the now-complete archive.
7. **Skip** device_meta; **purge** `ui_smoke_dev` from ha-2.
8. Re-verify: per-device coverage, span Jan 7→now, rungs non-empty, `cluster-doctor.sh`.
