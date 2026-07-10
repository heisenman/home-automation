# ADR-0026 — Canonical area taxonomy: one room registry, drift-guarded, history-safe renames

**Date:** 2026-07-04
**Status:** **Accepted** — canonical room set blessed by Hugh 2026-07-10; drift-guard GREEN; reconcile complete (added `staging` bucket for parked devices).
**Builds on:** the code-backed-doc + drift-guard pattern (ADR-0025 / `test_agents_nav`, ADR-0020 / `test_module_matrix`).
**Related:** ADR-0001 (dictator owns the registry), ADR-0014 R4 (area rollups / control-source), the
device-migrate maintenance primitive (`server/maintenance/device_migrate.py`).

## Context — areas are free-form strings, and they've already drifted

"Area" (the room a device lives in) drives real behavior: history **rollups**, house-**scene** grouping,
control-**source** selection (ADR-0014 R4), and **UI** room placement. Yet there is **no canonical definition
of the room set anywhere** — every config invents the string independently. They have diverged:

| Source | Areas used |
|---|---|
| **Sensor registry** `instance/devices.yaml` (10 rooms) | `attic, c_bedroom, c_office, crawlspace, h_bathroom, h_bedroom, kitchen, living_room, master_bathroom, master_bedroom` |
| **Control** `instance/control.yaml` | `living_room`, **`office`** ×3 |
| **Side-registries** `instance/*-devices.yaml` | **`office`**, **`infra`** |

`office` and `infra` **do not exist in the sensor registry** (which calls that room `c_office`). Areas are read
in ~15 `server/` modules; nothing validates them. The concrete trigger: registering the D1001 panel, a
headless agent had to **guess** the panel's area (it matched the existing `office` string) because there was no
source of truth to check — filing a live device under a room that doesn't exist for any sensor. Hugh has noted
the house-labeling scheme evolved a few times without a cleanup; this ADR is that cleanup, done systemically.

## Decision (proposed)

Make the room set a **single source of truth**, referenced everywhere and machine-checked — mirroring how
`REUSE.md`/`MATRIX.md` are generated-and-guarded.

1. **`instance/areas.yaml` = the canonical room registry** (Hugh owns its contents). Each area:
   `id` (the key used everywhere), `name` (display), and optional metadata (e.g. `floor`, `type`). A
   `config-examples/areas.example.yaml` ships the shape; the real file is gitignored like other `instance/`.
2. **Every area reference must resolve to an `areas.yaml` id.** devices.yaml, control.yaml, the side-registries,
   and any code area-enum reference a canonical id — no free-form room strings.
3. **A drift-guard test** (`tests/test_areas.py`, auto-discovered by `run_all.py`) asserts every area used in
   every config is in the canonical set. It will **fail loudly today** on `office`/`infra` — that failing test
   is the punch-list for the reconciliation.
4. **Renames are history-safe + gated.** Renaming an area is not a find-replace: like moving a device, it
   migrates history across **hot.db + parquet + rungs + registry** (the `device_migrate` lesson). An area
   rename becomes a scripted, idempotent, **gated** maintenance op (extend `server/maintenance/`), never a hand-edit.
5. **The dictator owns it** (ADR-0001): `areas.yaml` loads server-side; `device_registry` validates device
   areas against it; the viewmodel exposes the room list so the PWA/panel render rooms from the canonical set
   instead of whatever strings happen to appear.

## Rollout (architect now, migrate in a window)

- **Phase 0 — decide the taxonomy (Hugh + ops review):** Hugh blesses the canonical room ids + display names.
  Resolve the open questions below. *(This ADR is the vehicle for that conversation.)*
- **Phase 1 — land the source + guard (dev, cheap, safe):** add `areas.yaml` (canonical set) +
  `test_areas.py`. It goes **red** immediately on the current `office`/`infra` drift — intentional; it bounds
  the work and prevents new drift.
- **Phase 2 — reconcile (gated window):** the area-rename maintenance op migrates each drifted label to its
  canonical id (`office`→`c_office`?, `infra`→?) with history, then the guard goes green.
- **Phase 3 — UI (unblocked by the above):** PWA/panel render rooms from the canonical registry; the
  room-grouping cleanup Hugh deferred can finally land.

## Open questions for Hugh (Phase 0)

1. **Is `office` the same physical room as `c_office`?** (The D1001 panel, `levoit_office`, `host_210` are all
   `area: office`; the sensors call that room `c_office`.) If same → they collapse to `c_office`. If different →
   `office`/`c_office` are both canonical and the panel needs the right one.
2. **What is `infra`?** (a side-registry area) — a real room, or a non-room bucket (e.g. the rack/closet)? Do we
   allow non-room areas, or force everything into a physical room?
3. **Canonical ids + display names** — freeze the room list (the 10 above minus/plus your call) and their
   human labels. Any metadata worth carrying now (floor, indoor/outdoor)?

## Consequences

- Areas become a validated vocabulary; **an agent never guesses a room again** (the panel incident can't recur).
- The central registry **can't drift** — the test fails the moment a config names a room that isn't canonical.
- Small new surface: `areas.yaml`, `test_areas.py`, an area-rename maintenance op — all mirroring existing patterns.
- The panel/web-UI room cleanup Hugh deferred becomes tractable (it was blocked on there being a canonical set).
- **Acceptance is behavioral:** configs + UI all reference one room registry, and `test_areas` is green.
