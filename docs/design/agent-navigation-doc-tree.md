# RFC: agent-navigation doc tree (breadcrumbs + reuse discovery)

**Status:** DRAFT / RFC — ops-authored, out for dev review. Task: `modular-arch-and-agent-docs`.
**Goal (Hugh):** the project should *emphatically* push the docs + principles as critical to all future
work, **guide an agent to discover and reuse prior work** instead of reinventing it, and provide
**breadcrumb navigation** — start at a top map, tree downward — so agents don't constantly grep/search.

## Problem

We already have a mature `AGENTS.md` spine (root + 8 scoped: server/edge/provisioning/failover/tools/
tests/docs/firmware). Two gaps keep it from being a navigation system:

1. **No reuse/prior-art step.** Root "Starting a task" says *check board → read subsystem AGENTS → honor
   contracts*. It never says *find what already exists and reuse it*. Nothing points an incoming agent at
   the ~10 shared firmware modules, the edge event/adv contracts, or the `cmd/*` handler pattern. This is
   how wheels get reinvented (this session almost did: the whole loadable-profile deploy already existed as
   the `cmd/fs` handler + `ha_battery_profile`'s own contract; found by *reading*, not by process).
2. **The tree stops at the folder.** Subsystem AGENTS route to a directory, not down to the *modules* + their
   ADRs. Links point down but rarely **up**. So you still grep to find the right file, and from a file you
   can't climb to its authoritative parent.

## Design — two axes, thin center, heavy co-located edges

Two orthogonal ways to navigate, both rooted at the top map, both link-traversable (never grep):

- **By location (the tree):** root map → subsystem `AGENTS.md` → module. For "I'm working in X, what's here."
- **By capability (the index):** `docs/REUSE.md` + the ADR index. For "I need capability Y, what exists."

**Principle that makes it survive: keep the central map THIN, push detail to the co-located edges.**
A fat hand-maintained central map rots, and a stale map lies confidently (worse than none). The breadcrumb
therefore lives in the **module's own header** — it travels with the code and rots far slower. The central
spine holds only routing, which changes rarely.

## Deliverables

1. **Root `AGENTS.md` = the map.** Add (a) an emphatic **Principles (non-negotiable)** block at the very top —
   *docs-first · decompose/module-first · reuse-first · firmware-is-the-default-cause* — so it's the first
   thing any agent reads, repo-wide (today these live only in `firmware/AGENTS.md`); (b) a **reuse-first**
   standing contract + a "discover prior art" step in *Starting a task* (grep `firmware/components/`, scan the
   relevant ADR/design doc, check existing `cmd/*` handlers + `tools/` before building — reuse or justify).
2. **Bidirectional, complete-to-module `AGENTS.md` tree.** Each subsystem AGENTS links **up** to root and
   **down** to its key modules (one-liner each) + their ADRs. The leaf level (modules) is the current miss.
3. **Module-header breadcrumb convention.** One standard opening line per significant module:
   `<subsystem> › <module> — <purpose>. Contract: ADR-00xx. Parent: <subsystem>/AGENTS.md.`
   Modules already do a soft version (`ha_battery_profile.h` opens with "Versioned, loadable battery profile
   (ADR-0024 §5)…"); formalize the **up-link** so every header is a climbable breadcrumb. Document the
   convention once (root AGENTS or `docs/`), apply firmware-first.
4. **`docs/REUSE.md` = capability index.** Each shared module / contract / pattern with a one-line "reuse this
   when…". Worked examples from this session (loadable-profile reuse, `cmd/fs` SD pull, edge power envelope).

**Anti-rot:** tie "reconcile the map to reality" into the existing checkpoint discipline (`docs/CHECKPOINT.md`).
Open question below on whether that's enough or we want a mechanical check.

## Rollout

Firmware subsystem first as the template (that's where reuse matters most + the modules are cleanest), then
extend the pattern to server/ and edge/. Each step is a doc-only PR, independently reviewable.

## Questions for dev (where your view adds the most)

1. **Server-side navigation:** does `server/` (FastAPI/ingest/storage/control/cluster) have navigation needs the
   firmware-centric view misses? Is the tree→module granularity right for Python packages, or too fine?
2. **Header convention for Python:** is a co-located breadcrumb header realistic in the server code, or does it
   want a different form (package `__init__` docstring? a per-package `AGENTS.md`)?
3. **Anti-rot enforcement:** is checkpoint-discipline reconciliation enough, or should we add a mechanical check
   (a test that every `AGENTS.md` link resolves + every `firmware/components/*` and server package is indexed)?
   You own more of the test harness — is that cheap to add to `tests/run_all.py`?
4. **`REUSE.md` vs fold-in:** separate capability index, or fold into the directory map / each AGENTS? (ops leans
   separate — it's a different job than routing.)
5. **Prior art on the `.210` side:** is there any existing navigation/index material there I should build on
   rather than invent?
6. **Scope check:** is firmware-first-then-extend the right sequence, or should the root map + principles land
   for the whole repo in one pass first?
