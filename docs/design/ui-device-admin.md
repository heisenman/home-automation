# Design log — PWA device-admin (Services panel): UI-driven rename + relocate

Running design log for the `ui-device-admin` board task. Destined for an ADR once settled. Decisions +
why + rejected alternatives + gotchas.

## Goal

Let an operator **relocate** (change `area`) and **rename** (change `device_id`) a device from the PWA,
without a footgun. These are **Entity-plane** operations in the ADR-0034 model (Entity = `device_id` + `area`);
the panel is explicitly an Entity-plane admin surface (leaving room for a future Node-plane tab:
repoint/OTA/failover).

## True state at start (verified 2026-07-10, not the stale board note)

- **Backend logic — done.** `device_relocate.relocate()` and `device_migrate.rename_everywhere()` are
  importable, return report dicts, back up before mutating, are idempotent.
- **Orchestration — done.** `apply_rename_worksheet.run()` already wraps the *safe* flow: apply → pause
  `ha-reconcile-history` (peer-resurrection guard) → restart the ingest fleet → bounded idempotent sweep
  (migrate-while-live race) → clear old retained topics → verify (`test_areas` + 0 stale) → resume reconcile.
  It reads a **worksheet file**; the only missing piece is a **single-device** path into the same flow.
- **API — relocate endpoint exists but is a FOOTGUN.** `POST /api/v1/devices/{id}/relocate`
  (`control.py:handle_device_relocate`) calls `R.relocate()` directly — it does **not** restart services or
  sweep. So it mutates and then `ha-edge-mapper` re-stamps the old area on next ingest → **drift returns.**
  This is exactly the footgun we're removing; the fix is to route it through the orchestration too.
- **Rename endpoint — missing.**
- **UI — nothing** for rename/relocate. `DeviceMetaModal`'s "room" field is a *display overlay*
  (`control.db`, cosmetic), NOT the canonical registry area. Must not conflate.

## Decisions

1. **Invert away from "warn-only" restart → the endpoint runs the full orchestration and reports it.**
   (Hugh, 2026-07-10: "prefer to avoid footguns.") "Warn-only" also assumes shell access the primary user
   doesn't use. NOPASSWD on .210 already permits `systemctl … ha-*`, so the endpoint can restart the fleet.
2. **Reuse `apply_rename_worksheet`, don't re-author.** Refactor it to expose `run_plan(plan, …)` (the current
   `run()` = `load_plan` + `run_plan`), and a single-device plan builder. Both relocate AND rename route
   through it. One safe code path — no per-op restart-set drift.
3. **Fix the existing relocate endpoint** to use the orchestrated path (it currently doesn't restart/sweep).
4. **Rename/relocate are Entity ops (ADR-0034).** Guardrails baked in: Levoit/actuator area authoritative in
   `control.yaml` (ADR-0027); pure-actuator entities never "stale"; relay-carried entities relocatable by
   `device_id` transport-blind. Acceptance gate = `tools/model_project.py` (MODEL_LEAKS 0) before/after the
   dev tryout.
5. **Dry-run is synchronous** (preview, no mutation, no restart). The real apply is the async/detached path
   (see OPEN fork).

## RESOLVED fork (2026-07-10, Hugh) — Option A: detached job + poll

Mutating apply runs as a detached `systemd-run` transient unit that writes its report to a durable path
(`instance/db/admin-jobs/<id>.json`); the endpoint returns `{job_id}`, a status endpoint serves the report,
the UI polls. Dry-run preview stays synchronous. Survives the api restart; no long-held request.

### Job-launch mechanism — zero new sudo (verified 2026-07-10)

- Committed template unit `systemd/ha-admin-job@.service` (oneshot, `User=visko`, venv python, runs
  `python -m server.maintenance.admin_job run %i`). **No `NoNewPrivileges`** — the worker restarts the
  ingest fleet via `sudo systemctl restart ha-*` (existing NOPASSWD), which `NoNewPrivileges=yes` would
  break.
- API launches with `sudo systemctl start ha-admin-job@<job_id>.service` — the existing sudoers rule
  `/usr/bin/systemctl start ha-*` **already covers** the templated name (probed: no password prompt). So no
  new sudo grant, no `systemd-run`, no wrapper, no always-on daemon.
- The unit runs outside `ha-api`'s cgroup → survives the fleet restart the job itself triggers.
- Worker writes an atomic report JSON to `instance/db/admin-jobs/<id>.json`; `GET /api/v1/admin/jobs/<id>`
  serves it; the UI polls. Installing the unit file is a one-time `sudo cp` + `daemon-reload` (box-local on
  .210; part of the gated ha-2 deploy).

## OPEN fork — how to run the mutating orchestration (superseded by RESOLVED above)

The API **cannot cleanly restart itself** (the fleet restart includes `ha-api`/`ha-api-tls`), and the sweep
can take ~20–40s (`SWEEP_SETTLE_S`×passes) — too long to hold an HTTP request behind the `:443` bridge.

- **Option A — detached job + poll (LEANING).** Endpoint launches the single-device orchestration as a
  detached `systemd-run` transient unit that writes its report to a durable path
  (`instance/db/admin-jobs/<id>.json`); returns `{job_id}`. A status endpoint reads the report. Survives the
  api restart; no long-held request; matches the "long ops must be code-driven + detached + durable path,
  poll-only-to-validate-then-detach" directive. Cost: job-launch + status endpoint + UI polling.
- **Option B — synchronous, api-restart trails.** Run apply + non-api restart + sweep + verify in-request,
  return the report, then fire a detached oneshot to bounce `ha-api`/`ha-api-tls`. Simpler, immediate report,
  but holds the request ~20–40s (timeout risk) and is a second code path.

## Security once-over (2026-07-10)

- **Auth/gating:** rename/relocate/job-status all `Depends(require_admin)`; router mounts only on the VIP
  holder + master present (existing gate). Standby = not mounted.
- **FIXED — peer SSH shell injection via `device_id`.** The peer relocate/migrate step builds a *remote
  shell* command with `device_id` embedded in a double-quoted sqlite arg (over the full-shell `:22` SSH);
  it escapes SQL single-quotes but not shell double-quotes, and `validate_plan` only checks the *target*
  area. A crafted `device_id` (URL path) could break out. Fixed by validating the source `device_id` at the
  API boundary with `^[a-z0-9_]+$` (same guard as `new_id`).
- **job_id:** server-generated `uuid4().hex[:12]`; `status()` + the worker guard with `JOB_ID_RE` before any
  fs/systemctl touch (traversal test covers `../etc/passwd`). The `systemctl start ha-admin-job@<id>` arg is
  never user-supplied.
- **Info disclosure:** job reports carry ids/areas/backup-paths/service names — no secrets; admin-gated.
- **Known/accepted:** repeated launches each restart the fleet (admin-only thrash risk) — a "one active job"
  guard is a possible follow, not a v1 blocker.

## Flow of work (agreed)

1. Backend on .210 dev: `run_plan` refactor + single-device plan builder + rename endpoint + fix relocate
   endpoint; the OPEN-fork mechanism; host tests. `model_project.py` gate.
2. UI: Services panel with uniform preview (dry-run) → confirm → apply → report; surface peer status,
   backups, per-service restart results.
3. Live tryout on `.210:8443` on a disposable/test device — relocate first, then rename; verify no drift
   *after* the automated restart+sweep; `model_project.py` clean.
4. Ship: PWA cache bump + ES-module validation; commit/push. Prod (ha-2) rollout is a separate **gated**
   follow (air-gap deploy discipline, HA2-DEPLOY-PENDING); Hugh ensures no parallel dev during that window.

## Status

- **Phase 1 (backend) — DONE** @460b5ed. Tested (no new suite failures) + live launch→unit→worker→report
  smoke on .210 (no-op spec, no fleet bounce).
- **Phase 2 (UI) — DONE.** Relocate + Rename in `DeviceMetaModal` (`server/web/app.js`), preview→confirm→
  apply→poll via `MaintResult`; overlay-room vs canonical-area distinction spelled out inline. Both relocate
  modes offered (Hugh: "good applications for both"); **no default — explicit pick required** (Relocate
  disabled until restamp|forward chosen), matching the backend's required `mode`. sw cache v37→v38; app.js
  validates as an ES module; dry-run preview verified in-process against the real registry (relocate +
  rename plans correct, no-op rejected, no writes). Job-status route/poll path reconciled to
  `/api/v1/devices/jobs/<id>`.
- **Phase 3 (live dev tryout) — Hugh-gated.** Needs ha-api + ha-api-tls restarted to load the new endpoints,
  then a real relocate/rename of a disposable device (restarts the ingest fleet); `model_project.py` clean
  before/after.
