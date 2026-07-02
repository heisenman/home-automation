# ADR-0021 — Repo Documentation Tree for Agent Navigation

**Date:** 2026-07-01
**Status:** Proposed

## Decision

Lay a **navigation layer** over the (rich but scattered) existing docs: a hierarchy of `AGENTS.md` files from
the repo root down through each subsystem, plus a **`SKILLS.md`** index of the how-to runbooks. A fresh
LLM/agent session reads the root `AGENTS.md` first and can traverse to any subsystem's context in one hop,
without re-discovering the 42 KB architecture plan, the 21 ADRs, and the per-dir READMEs on its own.

## Context

The repo holds deep documentation — `home-automation-architecture-plan.md` (42 KB), `docs/adr/` (21 ADRs),
`ROADMAP`, `FOLLOWUPS`, `CHECKPOINT`, `device-onboarding`, per-subsystem READMEs, `edge/FIRMWARE-GUIDE.md`,
`failover/failover-runbook.md` — but **no agent entry point**. Nothing tells a new session "start here, this is
the model, here's where things live, here are the contracts you must not break." Every chat re-derives it.
There is also no `AGENTS.md`/`CLAUDE.md`/`SKILLS.md` anywhere in the tree today.

`AGENTS.md` is the portable, in-repo convention (read by Claude Code and other agent tools; travels with the
repo to cloud runs and other machines) — complementary to the user-global memory, which does not travel.

## Design

```
AGENTS.md                 root: "new chat starts HERE"
├─ system model (dictator authority, ADR-0001) + directory map
├─ standing contracts & gotchas (dumb-relay ADR-0001; gated prod writes;
│  .245 = sacred fileserver; secrets-out-of-git) + ADR index + SKILLS link
├─ server/AGENTS.md       BFF / ingest / viewmodel / control / storage (ADR-0006/0011/0013/0017)
├─ edge/AGENTS.md         firmware modules + MODULES.md + MATRIX.md (ADR-0001/0015/0020)
├─ tools/AGENTS.md        coord board, node_bringup, edge_ota/sign, enroll_node
├─ failover/AGENTS.md     keepalived/VRRP, reconcile, drill (ADR-0016/0018)
├─ provisioning/AGENTS.md device recipes (server, reterminal, levoit, openwrt, ntfy)
├─ tests/AGENTS.md        run_all.py + what each suite guards
└─ docs/AGENTS.md         how ADRs / decisions / retros / roadmap are organized
SKILLS.md                 index of how-to runbooks (build a node, flash a C6, run a drill…)
```

- **Root `AGENTS.md`** = terse orientation + routing; it points *out* to the deep docs, never duplicates them.
- **Nested `AGENTS.md`** = local orientation: what's here, key entry points, contracts, gotchas, relevant ADRs.
- **`SKILLS.md`** = the recurring runbooks cataloged (FIRMWARE-GUIDE, C6-SLAVE-FLASH-PROCEDURE, failover-drill,
  device-onboarding, `node_bringup`), so the right recipe is one hop away.

## Keeping it straight (anti-drift)

Three reinforcing mechanisms — the same principle at every layer:
1. **Short + local + code-adjacent** — a subsystem's `AGENTS.md` lives with its code, edited in the same diff.
2. **Code-backed indexes** — the ADR index derivable from `docs/adr/` filenames; the device×module `MATRIX.md`
   generated from `CMakeLists REQUIRES` (ADR-0020); `SKILLS.md` entries point at real runbook files. Indexes
   that can be regenerated can be checked, so they can't silently lie.
3. **Checkpoint discipline** — reconciling these docs is already a standing order (see the checkpoint routine
   in `docs/CHECKPOINT.md`); the `AGENTS.md` tree becomes part of what a checkpoint verifies.

## Consequences

- A new chat/task is productive in one read instead of a discovery crawl.
- New devices/subsystems get a column/node, not a re-explanation.
- Adds a small maintenance surface (the tree) — bounded by keeping files short, local, and code-backed.
- Complements, does not replace, the architecture plan and ADRs (root routes to them).

## Rejected alternatives

- **One giant top-level guide:** grows stale, too big to keep honest, doesn't localize to subsystems.
- **Rely on the user-global memory only:** doesn't travel with the repo (cloud runs, other devs, other tools).
- **No convention (status quo):** every session re-derives the map; contracts get missed.
