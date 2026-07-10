# ADR-0031 — Post-migration convergence: full data intake to ha-2 + a box-local dev failover on .210

**Status:** Proposed (2026-07-09) — anchors the migration-cleanup effort; sections may be split into
execution runbooks as Pillar 1/2 proceed.

**North star (goal, not this decision):** the production system (ha-2, air-gap dictator) must eventually
**run itself with no AI oversight** — hands-off and hardened. The 2026-07 air-gap migration was fast and did
**not** achieve that. This ADR records **step one**: making ha-2 the *complete, self-sufficient*
record-of-record. Real hardening (security posture, robustness, no-oversight resilience) is later, larger
work and is explicitly out of scope here — do not read this ADR as "the system is now hands-off."

---

## Context

The migration moved every device onto the air-gap net under dictator **ha-2 (192.168.1.210)**. `.210`
(household `192.168.0.210`, VIP `.200`, standby `.245`) remains the **permanent dev platform + web bridge**
(ADR-0021 / memory `210-dev-platform-two-endpoints`). Two problems surfaced in cleanup:

1. **Real house data is stranded on the wrong box.** ha-2 is *not* a superset of `.210`. Verified
   2026-07-09 (`instance/db`):

   | Class | `.210` (old dictator) | ha-2 (prod) | Gap |
   |---|---|---|---|
   | readings | 120,265 | 204,408 | both ways (ha-2 busier since; `.210` holds pre-migration span) |
   | summaries (derived) | 808 | 80 | ha-2 short ~728 |
   | rollup rungs | 47M | **absent** | entire tier missing on ha-2 |
   | weather history | 3.7M | **absent** | entirely stranded on `.210` |
   | control/actuator log | 51,234 | 45,266 | both ways (pre-migration actuations on `.210`) |
   | device_meta | 17 | 4 | ha-2 missing 13 |
   | mesh topology | links 142 | smaller | `.210` richer |
   | parquet archive | 16M | 16M | reconcile-covered |

   The reconcile machinery (`failover/reconcile-history.sh`, `reconcile-parquet.sh`) only covers the
   **sensor tier** (readings + parquet). Rungs, weather, summaries, device_meta, and the *divergent*
   control_log have **no merge path** today — `control.db`/`mesh.db` are handled as *snapshots* (overwrite),
   which in a two-way merge would destroy one side's history.

   *(The `.210` `instance/db` is 12G, but 11.9G of that is `instance/db/backups/` — 1,328 redundant
   pre-mutation DB snapshots from dev churn, not house data. Real data is ~120M.)*

2. **No air-gap failover.** ha-2 is the sole air-gap dictator; the household had a warm standby + VIP.

## Decision

Two pillars. **Pillar 1 is production-relevant; Pillar 2 is a dev convenience with a hard carve-out.**

### Pillar 1 — Full data intake `.210 → ha-2` (production-of-record completeness)

Converge **all real house data** into ha-2 as the record-of-record — a **bidirectional union** (each box
holds rows the other lacks), covering every class in the table above. **Production receives only real data +
the real code/config it depends on to run standalone** — never dev cruft (backups, migration tooling, dev
experiments), which stay on `.210`.

- **Sensor tier** (readings, parquet): existing `reconcile-history`/`reconcile-parquet` (union-safe).
- **Control/actuator, rungs, weather, summaries, device_meta, mesh:** need **row-level union merge**
  (natural-key/timestamp), *not* snapshot-overwrite — or, for rungs, a rebuild-from-readings pass. Each
  class gets an explicit strategy in a data-intake inventory before it touches real data (no silent
  overwrite). Derived metrics are primary data too (memory `data-storage-is-primary`).
- **Code/config self-sufficiency:** ha-2 is air-gapped (no `git pull`) — ensure its checkout + every
  dependent config/secret/dep is current locally (rsync from `.210`), so it runs without leaning on `.210`
  or on operator presence.

### Pillar 2 — `.210` as a box-local air-gap warm standby (DEV CONVENIENCE — NOT production)

`.210` doubles as ha-2's warm standby **only so there is a live failover target to develop against.** It is
**not** a production requirement.

> **Carve-out (normative):** the GitHub project MUST NOT require or expect an air-gap bridge computer, nor
> expect any one computer to participate in two systems at once. The straddle lives entirely in **box-local,
> gitignored `instance/` config + a second local checkout** — never committed. Repo edits stay generic
> (e.g. parameterizing unit names), and never prescribe a two-system box. The real production air-gap
> standby is a future dedicated NUC (memory `210-dev-platform-two-endpoints`).

Full separation — two independent implementations sharing only the box (and code binaries):

| Concern | Dev dictator (exists) | Air-gap failover (box-local, new) |
|---|---|---|
| Folder tree | `~/home_automation` + its `instance/` | separate checkout `~/ha-airgap-standby/` + own `instance/` |
| Dataset | `instance/db/hot.db` (dev) | its **own** mirror DB (seeded from complete ha-2) |
| Processes | `ha-*.service` | distinct prefix `ha-ag-*.service` |
| Broker | mosquitto bound household-side | own mosquitto bound air-gap-side (`.1.245` + VIP `.1.200`) |
| VRRP | VRID 51, `enp4s0`, VIP `.200`, MASTER | VRID 61, `wlp2s0`, VIP `.1.200`, BACKUP |
| Reconcile | `.210 ⇄ .245` | `ha-2 ⇄ .210` (standby store only — never the dev store) |

keepalived is the one unavoidable shared process (a host-level VRRP arbiter): a **single daemon with two
independent `vrrp_instance` blocks** (distinct names/VRIDs/interfaces/notify paths). Every other process is
fully separate. `notify.sh` is parameterized (`CONTROLLER_UNIT`/`RELAY_COORD_UNIT`, **plus API/edge-mapper
unit names**) so an air-gap transition never restarts the dev stack's units.

**Ordering:** Pillar 1 first (make ha-2 complete), *then* seed the standby from the complete ha-2, *then*
the ongoing `ha-2⇄.210`-standby reconcile. The ongoing loop is standby-store-only, so dev experiments never
leak into production.

**Client repoint (phased):** air-gap clients currently address ha-2's real `.1.210`, not the VIP; a
failover floats `.1.200` but clients won't follow until repointed. Repoint is deferred until the box is
properly configured and the standby is proven (per Hugh) — the warm mirror + reconcile is the real
resilience win first.

## Consequences

- ha-2 becomes the **complete** house record; nothing pre-migration is stranded on a box that is now "just
  dev." Requires building row-level merge for the non-sensor classes (real work, done before touching data).
- `.210` gains a genuine, always-on failover target for development — at the cost of running two isolated
  stacks. Keepalived carries two instances.
- The repo stays production-pure: it neither documents nor depends on the straddle.
- This is **one step**, not the hands-off/hardened end state.

## Rejected alternatives

- **Converge into one store on `.210` / retire the household cluster** — rejected: Hugh wants a full dev
  dictator *and* a full failover, kept separate.
- **Snapshot-overwrite control/mesh into ha-2** — rejected: destroys the diverged side's real history.
- **Two keepalived daemons** — unnecessary; one daemon, two `vrrp_instance` blocks is the supported form.
- **Commit the `.210` straddle into the repo** — rejected by the Pillar-2 carve-out.
- **Treat this intake as "the system is now self-sufficient"** — rejected: it is step one toward that goal.
