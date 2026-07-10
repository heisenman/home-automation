# Data-intake inventory — dev system → ha-2 (post-migration convergence, Pillar 1 of ADR-0031)

**Goal:** make the **ha-2 dictator** the *complete* record-of-record for the real house — intake every class
of real data currently stranded on the **dev system** (`.210` `~/home_automation`), then let it mirror to the
ha-2 failover. Production receives **only real data + real dependent config**; dev cruft stays on the dev
system. Terms per memory `airgap-standby-is-dev-convenience`. This is **step one** toward a self-running
production system (memory `prod-self-sufficient-is-the-goal`) — not the hardening itself.

## Method — sandbox-first (Hugh, 2026-07-09)

We control all boxes, and the data is small (~60M), so **no synthesis touches the live ha-2 dictator until
it is verified**:
1. Copy ha-2's `parquet` + `hot.db` + `control.db` (+ dev's `weather.db`) into a scratch dir on the dev box.
2. Run every merge/rebuild on the **copies**.
3. Verify (row counts, device coverage, span, no dup keys, no dev noise).
4. **Only then** copy the verified results back to the ha-2 dictator (atomic per-partition / table swap).

Safety invariants: **additive-only** (row-level DISTINCT union keyed `(device_id, ts, metric)` — the same
idempotency contract as ingestion; a re-merge is a no-op); **never snapshot-overwrite** a diverged table;
**derived data is rebuilt on ha-2, not transferred**; readings verified **all-real** (19 real device_ids,
zero synthetic/test) so no dev noise crosses over.

## Inventory (verified 2026-07-09/10)

| Class | dev system | ha-2 dictator | Real gap | Method | Risk |
|---|---|---|---|---|---|
| **parquet archive** (deep sensor history) | 10,707,331 rows · Jan 7–Jul 8 · 23 dev | 10,623,981 · same span · 20 dev | **3 gas devices + ~83k rows** | `reconcile-parquet` row-union → copy-back | none (additive) |
| ↳ stranded devices | `gas_h_office`, `gas_hbed`, `gas_standby` | absent | their full back-history | included in the union above | — |
| **rungs** (rollups, derived) | 47M (`rollup_meta`,`rung`) | **absent** | entire tier | **REBUILD on ha-2** from its now-complete parquet (reconcile-parquet's rebuild step) | none (regenerated) |
| **hot.db readings** (rolling ~1 day) | 120,265 (Jul 9–10) | 204,408 (Jul 9–10) | current-window divergence only | full-range export → `INSERT OR IGNORE` (unique index confirmed on ha-2) | none (additive) |
| **summaries** (derived, in hot.db) | 808 | 80 | 728 | rebuild on ha-2, else additive union | low |
| **weather.db** (external, irreplaceable) | 3.7M (`weather`) | **absent** | all of it | **transfer** to ha-2 (ha-2 has none) | low (new table) |
| **control.db `control_log`** (actuator history) | 51,234 | 45,266 | both ways (pre-migration actuations) | **custom row-level union** (natural key) — sandbox-tested | med (no tool yet) |
| **control.db `device_meta`** | 17 | 4 | ha-2 missing 13 | review — merge real-device rows; ha-2 owns canonical | med (config) |
| **mesh.db** | links 142 | smaller | topology | merge/rebuild (low priority) | low |

**Explicitly NOT intaken (stays on the dev system):** `instance/db/backups/` (12G, 1,328 pre-mutation dev
snapshots), migration/relocate tooling, and the dev system's *ongoing* readings (real but redundant/
dual-heard; the ongoing reconcile is ha-2-dictator ⇄ ha-2-failover only, never dev → ha-2).

## Order

1. **Sensor tier** (parquet union + rung rebuild + hot-window union) — safe, additive, ~99% already there.
2. **Irreplaceable externals** — `weather.db` transfer; `control_log` custom union; `device_meta` review.
3. Verify ha-2 completeness (span, per-device coverage, `cluster-doctor.sh`).
4. Then Pillar 2 (seed the ha-2 failover from the now-complete dictator).

Each step: sandbox → verify → copy-back. Hold before the custom `control_log`/`device_meta` merges for review.
