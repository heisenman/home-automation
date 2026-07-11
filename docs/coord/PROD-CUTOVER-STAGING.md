# PROD-CUTOVER-STAGING.md — device-admin + taxonomy → production (coordination)

**Status:** coordination / staging doc (ephemeral — not an ADR). **Date:** 2026-07-11.
**Purpose:** two big lifts are built + committed but **not yet in air-gap production (ha-2)**. This doc
captures their current state, the pending live-validation step, and — the part that matters most — the
**taxonomy dependence** between them, so the **next job** can plan the *safe + efficient order* to migrate
everything into production.

The two lifts:
- **Lift A — UI integration** (`ui-device-admin`): canonical device rename/relocate from the PWA.
- **Lift B — taxonomy update** (ADR-0034): the Node/Ability/Entity device object model + its `classify()`
  transport-plane classifier + (deferred) registry convergence.

Neither is fully live on ha-2. **`git-committed ≠ deployed-on-ha-2`** — ha-2 is air-gapped; code arrives by
a deliberate scp step, tracked in [HA2-DEPLOY-PENDING.md](../airgap/HA2-DEPLOY-PENDING.md).

---

## Lift A — `ui-device-admin` (UI integration)

**What it is:** relocate (change `area`) and rename (change `device_id`) a device from the PWA edit modal,
built so a UI-driven op **cannot** leave the ingest fleet re-stamping the old value (the footgun in the old
direct-mutation relocate endpoint).

**Done + committed (this checkout), Phases 1–2 + review refinements** — commits `460b5ed`..`a253a6d`:
- **Backend (`460b5ed`):** `apply_rename_worksheet.run_plan()` + `single_device_plan()` reuse the proven
  mutate→restart→sweep→verify orchestration (reconcile paused = the peer-resurrection guard). A real op runs
  **detached** via `sudo systemctl start ha-admin-job@<id>` (covered by the *existing* `systemctl ha-*`
  NOPASSWD — zero new grant), survives the ha-api self-restart, reports to a durable JSON polled at
  `GET /api/v1/devices/jobs/<id>`. `control.py` adds the `rename` endpoint + **re-routes the existing
  `relocate` endpoint** through the orchestration (it was the footgun — direct-mutate, no restart). Security:
  source `device_id` boundary-guard `[a-z0-9_]+` (peer-SSH shell-injection defence).
- **UI (`c012dea` + refinements):** "Maintenance" section in the edit modal — preview (dry-run, synchronous)
  → confirm → apply (detached) → poll → report. Refinements from Hugh's review: modal scroll fix
  (`82c475f`), sensor-tile ✎ (`29141c3`), canonical-area **dropdown** from `/api/v1/rooms` (`7a142be`),
  z-index/label fixes (`c128b45`,`20dad0e`), and a **scope refactor** (`a253a6d`): two tiers — "Display ·
  this device only" vs "Location & identity · canonical", Rename behind an Advanced/danger disclosure,
  per-device room-label overlay **removed** (vestigial).
- **Validation:** full test suite green (no new failures); `app.js` passes `node --check`; dry-run preview
  verified in-process against the real registry (no writes); detached launch→unit→worker→report smoke-proven
  on .210 with a no-op spec (no fleet bounce). Design log: [ui-device-admin.md](../design/ui-device-admin.md).

**NOT done — the pending live-validation step (Phase 3, dev), Hugh-gated:**

> **Phase 3 — live dev tryout on .210 (`:8443`):**
> 1. **Baseline:** `tools/model_project.py` → expect `MODEL_LEAKS: 0` (taxonomy acceptance gate).
> 2. **Load the endpoints:** restart `ha-api` + `ha-api-tls`, **pausing the failover healthcheck across the
>    restart** so it can't trip a spurious failover (`ha-api.service` notes; same discipline the
>    orchestration uses for reconcile). Confirm the control plane mounts + the new endpoints respond.
> 3. **Reversible real op on a low-stakes device** (candidate: `meter_pro_h_bed`): relocate → watch the job
>    to `done` → verify no drift + it keeps **logging on the destination** (the silent-drop check,
>    [[migrated-device-silent-drop]]) → relocate **back**. Optional rename round-trip (`<id>`→`<id>_test`→
>    `<id>`).
> 4. **Post-check:** `model_project.py` clean again; device registered + logging.
> 5. Clean → device-admin proven on dev; only the gated ha-2 prod rollout remains.

**Committed-not-deployed-to-ha-2 artifacts (Lift A):**
- `server/api/control.py` — new/changed endpoints (**behavior change**: ha-2's relocate is still the old
  footgun until deployed).
- `server/maintenance/apply_rename_worksheet.py` — `run_plan`/`single_device_plan`.
- `server/maintenance/admin_job.py` — **new module**.
- `systemd/ha-admin-job@.service` — **new unit; must be `sudo cp` + `daemon-reload`'d on ha-2** (not just scp'd into the checkout).
- `server/web/{app.js,styles.css,sw.js}` — PWA shell (sw cache at **v44**; a bump forces client refresh).
- These are **not yet rows in HA2-DEPLOY-PENDING.md** — adding the ledger rows + `HA2-DEPLOY-DRIFT` markers
  is part of the migration job (keeps the tripwire honest; the relocate behavior-change is the sharpest one).

---

## Lift B — taxonomy update (ADR-0034)

**What it is:** the device object model — **Node** (physical unit / transport plane) *hosts* **Abilities**
(CONFORMANCE catalog A–K) which *compose* an **Entity** (`device_id`+`area` / semantic plane). Category lives
on the Ability, never the Node. Reference: [DEVICE-MODEL.md](../DEVICE-MODEL.md) · [ADR-0034](../adr/ADR-0034-device-object-model-node-ability-entity.md).

**State:**
- Model + descriptive acceptance test **done**: `tools/model_project.py` (MODEL_LEAKS 0 on this checkout + a
  drifted standby).
- `device_push.classify()` re-framed as a transport-plane Node classifier — **committed-not-deployed**
  (ledger id `adr0034-classify`, marker in `device_push.py`, commit `10235b8`).
- **Phase 3 (registry convergence) — DEFERRED.** Would reshape *how* the Node→Entity join is stored.

**Committed-not-deployed-to-ha-2 artifacts (Lift B):** `device_push.py` (`adr0034-classify`, already in
ledger); `model_project.py` + `DEVICE-MODEL.md` deploy status **TBD** (needed on ha-2 to *validate* Lift A
there — see below).

---

## The taxonomy dependence (the crux)

**Short version: taxonomy is NOT a hard prerequisite to build or deploy device-admin — rename/relocate are
Entity-plane ops on the *current* registry shape — but the two are coupled in ways that dictate migration
*order*.**

1. **Rename/relocate ARE Entity-plane ops.** Entity = `device_id`+`area`; relocate changes area, rename
   changes device_id. Both operate on the current per-transport registries (`devices.yaml`/`control.yaml`/
   `*-devices.yaml`) which *are* the Node→Entity join table. They do **not** need Phase 3 convergence. → Lift
   A can deploy on today's registry shape without waiting on Lift B's deferred piece.

2. **Shared acceptance gate = `model_project.py` (a Lift-B artifact).** It's the pre/post check that a
   device-admin op introduced no model **leak / stale / orphan**. To validate a rename/relocate **on ha-2**,
   `model_project.py` must be present + green there. → the taxonomy *tooling* should land **on or before**
   device-admin validation in prod.

3. **Shared substrate = the registry.** Both lifts read/mutate the same registries. **If Phase 3 registry
   convergence is ever pulled into this push, it reshapes the files device-admin's tools parse**
   (`apply_rename_worksheet`/`device_relocate`/`device_migrate`) → those tools must be **re-validated AFTER
   convergence**. If Phase 3 stays deferred (recommended), no such coupling — device-admin deploys on the
   current shape. → **decision the migration job must make: is Phase 3 in scope for this cutover or not?**
   (Strong lean: NO — keep it deferred; it's a large air-gap-prod registry reshape with near-zero UI payoff.)

4. **A failover-rebuild triggers BOTH.** `classify()` (Lift B) and device-admin's migration primitives (Lift
   A) are both exercised by a **device migration / failover-rebuild run *from ha-2***. classify() aborts LAN
   actuators at `unknown` if stale; device-admin's relocate re-stamps-and-reverts if its endpoint fix is
   stale. → deploy them **together**, or the rebuild path is half-patched. (They're otherwise independent —
   rename/relocate never call `classify()`.)

5. **Taxonomy is device-admin's correctness frame.** The guardrails device-admin encodes come *from* the
   model: Levoit/actuator area authoritative in `control.yaml` (ADR-0027); pure-actuator entities never
   "stale"; relay-carried entities relocatable by device_id transport-blind. A divergent taxonomy would
   invalidate these assumptions. → the model must not drift out from under device-admin between now and
   cutover.

**Net for ordering:** deploy `model_project.py` (+ `DEVICE-MODEL.md`) to ha-2 **before/with** device-admin so
prod ops are gate-checkable; deploy `classify()` **together with** device-admin (shared failover-rebuild
trigger); keep **Phase 3 deferred** unless the migration job explicitly decides otherwise (if it does,
Phase 3 must precede device-admin re-validation).

---

## Committed-not-deployed inventory (feeds the migration)

| item | artifacts | in ledger? | notes |
|------|-----------|-----------|-------|
| `adr0034-classify` (Lift B) | `device_push.py` | **yes** | failover-rebuild-from-ha-2 is the forcing trigger |
| device-admin backend (Lift A) | `control.py`, `apply_rename_worksheet.py`, `admin_job.py`, `ha-admin-job@.service` | **no — add** | relocate endpoint behavior-change; unit needs install+daemon-reload |
| device-admin PWA (Lift A) | `server/web/{app.js,styles.css,sw.js}` | **no — add** | served on the prod front; sw cache bump = client refresh |
| taxonomy tooling/docs (Lift B) | `model_project.py`, `DEVICE-MODEL.md` | **TBD** | needed on ha-2 to validate Lift A in prod |
| `adr20-ota-propagate` (separate) | firmware fleet | board task | low-pri; fold into a taxonomy/tree uplift |

---

## THE NEXT JOB — plan the migration order (safe + efficient)

**Goal:** produce an **ordered cutover runbook** that moves all of the above into ha-2 (air-gap prod) in the
**safest AND most efficient** order — one deploy window, minimal restarts, each step verified-on-ha-2 before
the next.

**Constraints / inputs to honor:**
- Air-gap deploy discipline: **scp not git**, **verify the DEPLOYED file on ha-2** (don't trust the commit),
  drive off [HA2-DEPLOY-PENDING.md](../airgap/HA2-DEPLOY-PENDING.md) + [AIRGAP-MIGRATION.md](../airgap/AIRGAP-MIGRATION.md).
- ha-2 self-sufficiency north star (prod runs with no AI oversight); repo stays production-pure; the .210
  straddle is dev-convenience only.
- Both APIs restart → failover-blip discipline (pause healthcheck); control plane is VIP-gated (mounts only
  on the VIP holder + master present).
- Hugh ensures **no parallel dev** during the prod cutover window.
- Ordering decisions the job must settle: (a) Phase 3 in scope? (lean no); (b) sequence of
  model_project.py → classify()+device-admin → PWA → verify; (c) reversible checkpoint/rollback per step;
  (d) ledger rows + `HA2-DEPLOY-DRIFT` markers added/retired as each item deploys.

**Prereq before the prod cutover:** Lift A's **Phase 3 dev tryout** (above) passes green on .210.

---

## Cross-references

[ui-device-admin design log](../design/ui-device-admin.md) · [ADR-0034](../adr/ADR-0034-device-object-model-node-ability-entity.md)
· [DEVICE-MODEL.md](../DEVICE-MODEL.md) · [HA2-DEPLOY-PENDING.md](../airgap/HA2-DEPLOY-PENDING.md)
· [AIRGAP-MIGRATION.md](../airgap/AIRGAP-MIGRATION.md) · memories: `pwa-surface-maturity-map`,
`airgap-checkout-drift`, `migrated-device-silent-drop`, `prod-self-sufficient-is-the-goal`.
