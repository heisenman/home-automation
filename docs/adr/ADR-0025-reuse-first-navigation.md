# ADR-0025 — Reuse-First Agent Navigation: breadcrumbs, a generated reuse index, and a drift-guard

**Date:** 2026-07-04
**Status:** Proposed
**Extends:** [ADR-0021](ADR-0021-repo-documentation-tree-agent-navigation.md) (the `AGENTS.md` navigation tree).
Builds on [ADR-0020](ADR-0020-shared-edge-panel-firmware-core.md) (shared modules) and the code-backed-doc pattern
(`tools/gen_module_matrix.py` → `edge/MATRIX.md`, guarded by `tests/test_module_matrix.py`).
**Reviewed-by:** dev (STRONG ACCEPT, 2026-07-04) — see [agent-navigation-doc-tree.md](../design/agent-navigation-doc-tree.md) for the RFC + full review.

## Decision

Make **reuse the default and navigation grep-free**, and guarantee it mechanically:

1. **Emphatic principles at the root.** A *Principles (non-negotiable)* block at the top of the root
   `AGENTS.md`: **docs-first · decompose/module-first · reuse-first.** These are the first thing any agent
   reads, repo-wide. `firmware/AGENTS.md` **links up** to them (never duplicates → can't re-rot);
   `firmware-is-the-default-cause` stays firmware-scoped.
2. **A reuse-first gate.** *Starting a task* gains a "discover prior art" step: before building, search the
   reusable surfaces (`firmware/components/`, the relevant ADR/design doc, existing `cmd/*` handlers,
   `tools/`) — **reuse or explicitly justify not.**
3. **Two navigation axes, both rooted at the top map, both link-traversable (never grep):**
   - **Location** — the ADR-0021 `AGENTS.md` tree, made **bidirectional** (every node links *up* to its
     parent and *down* to its modules) and complete **to the module level**.
   - **Capability** — `docs/REUSE.md`: each shared module / contract / pattern with a one-line "reuse this
     when…".
4. **Module-header breadcrumbs (the leaf).** Each firmware component header carries one standard line —
   `<subsystem> › <module> — <purpose>. Contract: ADR-xx. Parent: firmware/AGENTS.md` — so from any file you
   can climb to its authoritative parent. For **Python the breadcrumb unit is the package**, expressed via the
   existing `server/AGENTS.md` package table (+ optional one-line `__init__` docstring) — **not** per-`.py`
   headers (too fine, high rot).
5. **`docs/REUSE.md` is GENERATED, never hand-maintained.** `tools/gen_reuse.py` builds it from the
   module-header breadcrumbs + `server/AGENTS.md`, mirroring `gen_module_matrix.py`. A hand-kept central index
   violates this ADR's own premise — *a stale map lies confidently.*
6. **A drift-guard test is the durability mechanism.** `tests/test_agents_nav.py` (auto-discovered by
   `tests/run_all.py`, mirroring `test_module_matrix.py`) asserts: (a) every `AGENTS.md` relative link
   resolves; (b) every `firmware/components/*` and server package is indexed in `REUSE.md`; (c) every
   module-header `Parent:` link resolves. **This is what makes the whole thing durable** — checkpoint
   discipline (ADR-0021's anti-drift) is exactly what failed to prevent the near-reinvention below.

## Context

The repo has deep docs and (via ADR-0021) a navigation *tree*, yet an agent still (a) can't easily **discover
what already exists to reuse**, and (b) navigates by grepping. This session nearly reinvented a whole
reflash-free profile-deploy path that **already existed** as the `cmd/fs` SD handler plus `ha_battery_profile`'s
own contract — it was found by *reading prior art*, not by any process the repo enforces. ADR-0021 lists
"code-backed indexes" and "checkpoint discipline" as anti-drift, but stops short of a **reuse orientation** and
a **mechanical check** — and checkpoint discipline is a human habit that already failed here. The fix is to
make reuse an explicit gate and to let a test, not a habit, keep the map honest.

## Design & rollout

**Thin center, heavy co-located edges.** The central spine (root + subsystem `AGENTS.md`) holds only routing,
which changes rarely; the breadcrumb lives in each module's own header, travelling with the code so it rots
far slower; the capability index is generated from those headers so it *cannot* drift.

Three independently-shippable passes (each a doc/test PR):
- **Pass 1 — repo-wide, highest-leverage (= the original ask):** root Principles block + reuse-first
  "Starting a task" step + `test_agents_nav.py` link-check. *The check must exist before links accumulate.*
- **Pass 2 — firmware-first:** module-header breadcrumb convention on the components + `gen_reuse.py` →
  `REUSE.md`; extend the test to the index + Parent-link assertions.
- **Pass 3 — extend:** server/edge package-level breadcrumbs via `server/AGENTS.md`; build on
  `docs/ENVIRONMENT.md` (build matrix) + the ADR index rather than invent.

Ownership: firmware side + Pass 1 → ops; server side (Pass 3) + the `run_all.py` test → dev.

## Consequences

- Reuse becomes discoverable **and enforced**, not a matter of whether the agent happened to read the right doc.
- The central capability index **can't lie** — it's generated and drift-tested, like `MATRIX.md`.
- Maintenance stays bounded: most content is co-located with code, and correctness is machine-checked rather
  than hand-reconciled.
- Operationalizes ADR-0021 → **ADR-0021 moves to Accepted** once Pass 1 lands.
- Small new surface: `gen_reuse.py`, `REUSE.md`, `test_agents_nav.py`, and the header convention — all mirroring
  patterns the repo already runs.
