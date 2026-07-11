# PROD-CUTOVER-RUNBOOK.md — ordered ha-2 cutover: device-admin + taxonomy + firmware OTA

**Status:** executable runbook (coordination). **Date:** 2026-07-11. **Owner:** dev2 (single-owner —
serialized, [[device-migration-single-owner]]). **Companion:** [PROD-CUTOVER-STAGING.md](PROD-CUTOVER-STAGING.md)
(the why + the taxonomy dependence). **Nothing here runs without Hugh's GO per phase.**

Moves three committed-not-deployed lifts into **ha-2** (air-gap prod) in one ordered, reversible sequence:
- **B — taxonomy:** `device_push.classify()` + `tools/model_project.py` + `docs/DEVICE-MODEL.md`
- **A — device-admin:** backend (`control.py`, `apply_rename_worksheet.py`, `admin_job.py`,
  `ha-admin-job@.service`) + PWA (`server/web/*`)
- **C — firmware OTA** (`adr20-ota-propagate`): deduped shared-component firmware → live edge fleet

**Topology (from the survey):** ha-2 = `192.168.1.210` (air-gap), dictator/VIP `192.168.1.200`.
`.210` is the dual-homed bridge (household `192.168.0.210` + air-gap `192.168.1.245`) and serves the
household HTTPS front (`:443` nginx) reverse-proxying to **ha-2's plain `ha-api:8123`** — **ha-2 has NO
`ha-api-tls`**. Code reaches ha-2 by **scp from .210**, never `git pull` ([[airgap-checkout-drift]]).

---

## Invariants (assert continuously — retro §A#1: verify STATE, not intent)

1. **Exactly one healthy dictator; ha-2 keeps the VIP throughout** (no accidental failover during the window).
2. **`git-committed ≠ deployed`** — every file's truth is its **checksum on ha-2**, not the commit.
3. **Data never silently stops** — `ha-writer` logging + every migrated entity **registered AND logging on
   ha-2** is re-proven after each mutating step ([[migrated-device-silent-drop]]).
4. **One owner, serialized, no parallel dev in the window** (Hugh's commitment; [[device-migration-single-owner]]).
5. **Every irreversible step = explicit GO gate + a machine-checked proof on the bus/box.**

## Global safety nets (what makes live-prod iteration safe — [[failover-primitives-not-on-vip]])

- **Checkpoint before (Phase 0):** ha-2 `hot.db` + `control.db` snapshot, `model_project.py --json` baseline,
  deployed-file checksums, `required_services` all-active, `.master_pass` **offline** backup.
- **Rollback per phase:** each phase lists its exact revert (scp the prior file back + restart). Code staging
  (Phase 1) is reversible by definition; the only load-bearing restart is Phase 2's `ha-api`.
- **Dead-man on the risky restart:** wrap the Phase-2 keepalived-pause in a transient timer that
  `systemctl start keepalived` after N min even if the session dies (pattern from `failover-drill.sh`).
- **Live rehearsal, not just component tests:** the meta-lesson — *only a live `--run` exposed a split-brain
  every manual test passed*. Phase 0's dev tryout + Phase 3's reversible prod round-trip ARE that rehearsal.

---

## PHASE 0 — Pre-flight (dev/.210 + ha-2 read-only; SAFE) — GO to proceed to Phase 1

**On .210:**
- [ ] `coord.py agents` + `coord-local.py roster` → confirm no live parallel dev; announce the window.
- [ ] **Verify-after-push:** `git fetch && [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ]`
      (a silent push-reject once diverged the bus — retro §B). `python3 tools/gen_module_matrix.py --check`
      → *in sync*. `venv/bin/python -m tests.run_all` → no NEW failures (know the pre-existing set).
- [ ] **venv sanity** ([[shared-venv-esphome-paho]]): `venv/bin/python -c "import paho.mqtt,sys;print(paho.mqtt.__version__)"`
      — a co-resident esphome install can pin `paho<2` and break `edge_ota`; note the version.
- [ ] **Device-admin DEV TRYOUT on .210 `:8443`** (the gated Phase-3 tryout from the staging doc — this is
      the rehearsal): `model_project.py` baseline → **stop keepalived** → restart `ha-api` **and**
      `ha-api-tls` (dev has both) → start keepalived → reversible relocate+rename round-trip on a low-stakes
      device (candidate `meter_pro_h_bed`) → verify no drift + registered+logging → `model_project.py` clean.
      **GO/NO-GO:** device-admin is not touched in prod until this is green.

**On ha-2 (read-only checkpoint — over :22 from .210):**
- [ ] Snapshot: `sqlite3 instance/db/hot.db ".backup /tmp/hot.pre-cutover.db"` + same for `control.db`;
      pull both to .210 offline.
- [ ] `model_project.py --json` on ha-2 → **MODEL_LEAKS 0** baseline (record it).
- [ ] `tools/required_services.py --check` (or the supervisor) → **all required units active** — especially
      `ha-writer`, `ha-edge-mapper`, `ha-levoit-bridge` (the last was silently missing for 14h once).
- [ ] **Back up `instance/.master_pass` OFFLINE** — it is `preposition` (never on the wire); losing it breaks
      control auth + failover. Confirm it exists on ha-2 before any restart.
- [ ] Confirm ha-2 holds the VIP (`vip_held`) and is MASTER.

**Rollback:** N/A (no changes). **Landmines:** [[bff-catalog-restart-both-apis]] applies to .210 (both APIs);
ha-2 is single-API.

---

## PHASE 1 — Stage code to ha-2 (scp + verify DEPLOYED files; NO behavior change) — GO to Phase 2

Staging files does **not** change running behavior (Python modules load at process start; services keep
running old code until Phase 2). The one exception is the PWA (served from disk per-request) — stage it
**last, immediately before Phase 2**, so the browser doesn't call endpoints that aren't loaded yet.

- [ ] On .210: `git pull` to HEAD; re-run `gen_module_matrix.py --check`.
- [ ] **scp each artifact to ha-2** (use `scp -O` — OpenSSH10 SFTP-subsystem vs ForceCommand,
      [[failover-ssh-decouple]]; over :22, the full-shell leg — **47222 is reconcile-only, cannot carry
      arbitrary code**):
  - B: `server/maintenance/device_push.py`, `tools/model_project.py`, `docs/DEVICE-MODEL.md`
  - A-backend: `server/api/control.py`, `server/maintenance/apply_rename_worksheet.py`,
    `server/maintenance/admin_job.py`
  - A-unit: `systemd/ha-admin-job@.service`
  - **Safety infra:** `failover/healthcheck.sh` (the maintenance-inhibit) — **must land on ha-2's checkout
    AND `~/ha-airgap-standby/failover/healthcheck.sh`** (the air-gap `chk_dictator_airgap` execs the standby
    checkout, not the repo — classic [[airgap-checkout-drift]]; verify BOTH deployed copies).
- [ ] **VERIFY DEPLOYED FILES on ha-2 — checksum/diff, NOT the commit** ([[airgap-checkout-drift]]):
      `for f in <paths>; do ssh ha-2 "sha256sum ~/home_automation/$f"; sha256sum $f; done` → must match.
  - Behavioral proof for classify(): `ssh ha-2 'cd ~/home_automation && python -c "from server.maintenance
    import device_push as D; print(D.classify(\"dehumidifier_living_room\"))"'` → **`local-driver`**
    (the fixed value; `unknown` = stale).
- [ ] **Install the job unit on ha-2:** `sudo cp systemd/ha-admin-job@.service /etc/systemd/system/ &&
      sudo systemctl daemon-reload` (a scp into the checkout is NOT enough — it must be in
      `/etc/systemd/system`). Smoke it with a **no-op spec** (rename id→same id ⇒ empty plan) → job unit runs,
      writes `done`/no-op, **no fleet restart** (proven pattern on .210).
- [ ] Stage the PWA last: `server/web/{app.js,styles.css,sw.js,index.html,manifest.webmanifest,icon.svg,vendor/*}`
      → verify checksums.

**Rollback:** the staged files are inert until Phase 2; to abort, scp the pre-cutover copies back (or just
don't restart). **Landmine:** if any scp path lands outside what the bridge/firewall allows, use the :22 leg
(open during transition) — do **not** try to force it through 47222.

---

## PHASE 2 — Cut over ha-2 software (load new code) — GATED, load-bearing restart

`classify()`, `model_project.py`, `apply_rename_worksheet`, `admin_job` are only invoked by tools / the
per-invocation job unit — **no service restart loads them**. The **only** service that must restart is
**`ha-api`** (to expose the rename/relocate/job endpoints + the fixed relocate). `classify()` + device-admin
ship together (shared failover-rebuild trigger — staging doc §dependence).

- [ ] **GO gate (Hugh).**
- [ ] **Hold the healthcheck FIT across the restart** — the CORRECT mechanism (the maintenance-inhibit built
      + proven on the .210 rehearsal; **NOT** `stop keepalived`, which would *yield* the VIP): on ha-2
      `touch instance/.maintenance-fit`. `failover/healthcheck.sh` then reports fit while the flag is fresh,
      so the `ha-api` restart can't drop priority → no spurious failover. **Dead-man:** the `MAINT_FIT_MAX_AGE`
      (300s) staleness bound auto-re-arms the healthcheck even if the flag is forgotten. (A device-admin *op*
      sets/clears this itself in `run_plan`; Phase 2 is a manual restart, so set it by hand.)
- [ ] `sudo systemctl restart ha-api` on ha-2. Wait for ready (`curl -sf localhost:8123/api/v1/sensors`).
- [ ] **Re-arm:** `rm instance/.maintenance-fit`; confirm VIP still on ha-2 (`vip_held`).
- [ ] **VERIFY on ha-2:**
  - Control plane mounted (VIP + master present); admin endpoints respond — **dry-run preview** of a
    rename/relocate returns `200 preview` (read-only, no mutation).
  - `model_project.py` → MODEL_LEAKS 0 (unchanged from Phase-0 baseline).
  - `required_services --check` all active; `ha-writer` still logging (tail a fresh reading).

**Rollback:** scp the Phase-0 `control.py` back → `stop keepalived` → `restart ha-api` → `start keepalived`.
**Landmines:** never restart `ha-api` without the keepalived pause (>10s restart can trip the 2-miss
healthcheck → spurious failover); ha-2 has **no** `ha-api-tls` — don't try to restart it.

---

## PHASE 3 — Prove device-admin live in prod (reversible) — GATED

- [ ] **GO gate (Hugh).**
- [ ] Reversible round-trip on a low-stakes device **on ha-2** (via `https://192.168.0.210/` or curl):
      relocate → watch the detached job (`ha-admin-job@<id>`) to `done` → **verify no drift + registered AND
      logging on the destination** ([[migrated-device-silent-drop]]; the mapper re-stamp + idempotent sweep +
      retained-clear are inside the orchestration — [[device-relocate-and-live-registry-restart]]) →
      relocate **back**. Optional rename round-trip (`<id>`→`<id>_test`→`<id>`).
- [ ] `model_project.py` clean after. This proves the full chain (endpoint → detached job → orchestration →
      verify) in production.

**Rollback:** the op is reversible by construction (round-trip); backups are written before each mutation
(`instance/db/backups/`, `docs/runbook-dataset-restore.md`).

---

## PHASE 4 — Firmware OTA to the edge fleet (`adr20-ota-propagate`) — GATED, serialized, physical-access risk

Touches edge **nodes**, not ha-2 services — lower risk to prod data, but has **cable-access landmines**. Do
it **after** Phase 2/3 are stable; it is deferrable to its own window without blocking the software cutover.

- [ ] **GO gate (Hugh).** Bench + USB programmer ready for the C6 fallback (below).
- [ ] Preconditions: `HA_CMD_SECRET` sourced from the node's `secrets.h`; venv `paho` version OK (Phase-0
      check); node-identity gate map current (`edge/esp32c6/nodes.yaml` node_id↔MAC).
- [ ] **Per node, ONE AT A TIME** ([[device-migration-single-owner]]):
      `tools/edge_ota.py --node <id> --bin <build> --serve-ip 192.168.0.210 --broker <net-broker>` →
      verify node reconnects, publishes `home/<area>/<id>/state`, relay coverage intact.
- [ ] **C6 dangling-node_id landmine** ([[c6-fleet-ota-reject]], [[edge-ota-node-id]]): deployed C6 gas nodes
      (`cbed/coffice/hbed/hoffice`) **reject the first OTA** (identity gate). Remedy in order: (a) **cable-flash
      first** with the correct `secrets.h` (node_id+MAC), then OTA normally; (b) `unknown@` break-glass OTA
      only if cable access is impossible (recovery path, not production-first). **Enrollment/identity is a
      named precondition, not a discovery** (retro §B — `HA_CMD_SECRET ""` once silently rejected commands
      mid-OTA).
- [ ] The cosmetic `edge/<device>/` → `firmware/devices/<device>/` tree move is **NOT** part of this window
      (it churns OTA scripts/manifests). Leave deferred.

**Rollback:** re-OTA (or cable-flash) the prior image; nodes are individually recoverable at the bench.

---

## PHASE 5 — Reconcile the ledger + record deployed state + close out

- [ ] **Retire tripwire items** ([[airgap-checkout-drift]]): for each deployed item, remove its
      `HA2-DEPLOY-DRIFT:<id>` code marker **and** its `HA2-DEPLOY-PENDING.md` row **in one commit** (the sync
      test enforces marker↔row). `adr0034-classify` retires here; **add-then-immediately-resolve** rows for
      device-admin (or leave a row for any part intentionally deferred).
- [ ] **Record the deployed commit on ha-2** (the missing version marker the survey flagged): write
      `instance/deployed-commit` = HEAD sha on ha-2, so "what does prod run" is answerable next time.
- [ ] **Post-cutover health:** `cluster-doctor` (exactly-one-controller, one VIP holder, fresh heartbeats) +
      `required_services --check` on **both** boxes; `model_project.py` clean; `ha-writer` logging; a live
      actuator command round-trips (**Midea LAN token** — test immediately, don't assume it survived).
- [ ] **Board + memory:** close `prod-cutover-order`; close/adjust `adr20-ota-propagate` (done or the
      remaining nodes deferred); update the checkpoint memory; `beacon`.

---

## Landmines & learnings baked in (provenance)

| Guardrail (where it lives above) | Source |
|---|---|
| Verify the **deployed file's checksum on ha-2**, not the commit; scp not git-pull; surgical | [[airgap-checkout-drift]] |
| **Verify-after-push** (`HEAD==origin/main`) before trusting the tree | retro §B (silent push-reject) |
| Migrated entity must be **registered AND logging on destination**; sweep live-LWT vs registered | [[migrated-device-silent-drop]], [[migration-ha2-registry-sync]] |
| Registry/area change ⇒ **full ingest-fleet restart + idempotent sweep + retained-clear** (in-orchestration) | [[device-relocate-and-live-registry-restart]] |
| **Gate irreversible + prove STATE on the bus**; invariant written first, asserted continuously | retro §A#1 |
| **Live rehearsal** (dev tryout + reversible prod round-trip) — component tests missed a live split-brain; **checkpoint/restore/dead-man** nets make it safe | [[failover-primitives-not-on-vip]], failover-drill |
| **Single-owner, serialized, one-at-a-time; no parallel dev; isolate early** | [[device-migration-single-owner]], retro §F#1 |
| **Enrollment/identity is a precondition**; fresh/dangling node rejects all OTAs; cable-flash first; validate on a recoverable node | [[edge-ota-node-id]], [[c6-fleet-ota-reject]], retro §B |
| Restart **only `ha-api`** on ha-2 (no `ha-api-tls` there); **pause keepalived** across it | survey §2, [[bff-catalog-restart-both-apis]], [[control-plane-vip-gated]] |
| `scp -O`; **47222 is reconcile-only** (use :22 for code); check **venv paho** version | [[failover-ssh-decouple]], [[shared-venv-esphome-paho]] |
| **`.master_pass` never on the wire** — back up offline before touching prod | survey §1 (preposition) |

*Verify-at-execution (survey claims to re-check on the box, not trust here): TLS cert `notAfter`, Midea LAN
token freshness, exact required-services count, port-22 open state.*
