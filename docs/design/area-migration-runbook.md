# Area-taxonomy migration — runbook (ops → dev handoff)

**Date:** 2026-07-04  ·  **Execution owner:** dev  ·  **Decision:** [ADR-0026](../adr/ADR-0026-canonical-area-taxonomy.md)
**Status:** taxonomy FROZEN (Hugh-blessed, Phase 0 complete) — this is the ordered *how* to land it.

ADR-0026 is the **why/what** (one room registry, drift-guarded, history-safe renames). This runbook is the
**ordered how**. The **canonical content** (the real 20-area room list + the old→new crosswalk + the floor-plan
map) is **private** — ops holds it and hands it over the board (`area-taxonomy`); it never enters this repo
(privacy rule below). Everything here is the generic, committable mechanism.

## Schema — `instance/areas.yaml`

One entry per area, keyed by `id` (the string every config + the code reference):

```
<id>:
  name:  <display label>     # what the UI renders
  level: main_floor | attic | crawlspace | …
  zone:  h_suite | c_suite | common | circulation | utility | exterior | none
  type:  kitchen | living | bedroom | bathroom | office | dining | entry |
         closet | mechanical | circulation | porch | monolithic
```

Load-bearing choices (from the ops review of ADR-0026):
- **`zone` is first-class**, not a tag — rollups + house-scenes target a whole zone ("quiet the `h_suite`").
- **Closets are addressable** — each closet is its own `id` (Hugh's call).
- **Non-room areas are allowed** — e.g. a network rack — via `type`; don't force everything into a room.
- **Monolithic levels** (attic, crawlspace) = a single area sitting at its own `level`.

## Generic example (the shape — a fictional house, safe to commit)

Ship this as `config-examples/areas.example.yaml`:

```yaml
areas:
  kitchen:       { name: Kitchen,       level: main_floor, zone: common,      type: kitchen }
  living_room:   { name: Living Room,   level: main_floor, zone: common,      type: living }
  hall:          { name: Hall,          level: main_floor, zone: circulation, type: circulation }
  suite_a_office:{ name: Office A,      level: main_floor, zone: suite_a,     type: office }
  suite_a_bed:   { name: Bedroom A,     level: main_floor, zone: suite_a,     type: bedroom }
  suite_a_bath:  { name: Bath A,        level: main_floor, zone: suite_a,     type: bathroom }
  suite_b_bed:   { name: Bedroom B,     level: main_floor, zone: suite_b,     type: bedroom }
  mech_closet:   { name: Mechanical,    level: main_floor, zone: utility,     type: mechanical }
  porch:         { name: Porch,         level: main_floor, zone: exterior,    type: porch }
  attic:         { name: Attic,         level: attic,      zone: none,        type: monolithic }
  crawlspace:    { name: Crawlspace,    level: crawlspace, zone: none,        type: monolithic }
```

## Ordered phases

### Phase 1 — land the source + guard  *(cheap, safe, no gate — do now)*
1. **`instance/areas.yaml`** — the real area set (get the 20-area list from the ops board handoff; real names, gitignored).
2. **`tests/test_areas.py`** — drift-guard, auto-discovered by `tests/run_all.py`: assert every area referenced
   in every config (devices.yaml, control.yaml, the `*-devices.yaml` side-registries, any code enum) resolves to
   an `areas.yaml` id. It goes **RED today** on the known drifters (`office`, `infra`) — *intended*; that failing
   set is the Phase-2 punch list and it blocks new drift.
3. **`config-examples/areas.example.yaml`** — the generic shape above (committable; the real file is gitignored).

**Acceptance:** `areas.yaml` loads server-side; `test_areas` exists and is red only on the known drifters.

### Phase 2 — reconcile  *(gated window)*
Build the **idempotent, history-safe, gated** area-rename op (extend `server/maintenance/`, mirror
`device_migrate.py`): migrate each drifted label to its canonical id across **hot.db + parquet + rungs +
registry + control.yaml + side-registries** — never a hand-edit. Apply the **crosswalk** (ops handoff — the full
keep/rename/merge list, incl. re-homing the D1001 panel's `area:office` and verifying `host_210`).

**Acceptance:** the op is re-runnable + gated; `test_areas` goes **GREEN**.

### Phase 3 — UI  *(unblocked by 1–2)*
The viewmodel exposes `level / zone / room + display`; PWA + panel render rooms from the canonical registry
(ends the hardcoded room strings; enables the room-grouping cleanup that was blocked on a canonical set).

## Privacy rule (hard)
The real `instance/areas.yaml`, the real room names, and the floor-plan map **never** enter this public repo.
They live in `instance/` (gitignored) + ops's private artifacts. Only the **schema, this runbook, and the
generic example** commit. Mirrors `repo-public-and-scrub`.

## Discoverables
- **Decision** → [ADR-0026](../adr/ADR-0026-canonical-area-taxonomy.md).  **How** → this runbook.
- **Real room list (20 areas) + old→new crosswalk + floor-plan (UI reference)** → ops handoff on board
  `area-taxonomy` (ask ops; kept off-repo by the privacy rule).
- **History-safe migrate precedent** → `server/maintenance/device_migrate.py`.
- **Drift-guard precedents** → `tests/test_agents_nav.py` (ADR-0025), `tests/test_module_matrix.py` (ADR-0020).
