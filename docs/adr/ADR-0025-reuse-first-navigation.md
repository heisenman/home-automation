# ADR-0025 — Reuse-First Agent Navigation: breadcrumbs, a generated reuse index, and a drift-guard

**Date:** 2026-07-04
**Status:** Accepted (2026-07-04; implementation tracked on the board as `modular-arch-and-agent-docs`)
**Extends:** [ADR-0021](ADR-0021-repo-documentation-tree-agent-navigation.md) (the `AGENTS.md` navigation tree).
Builds on [ADR-0020](ADR-0020-shared-edge-panel-firmware-core.md) (shared modules) and the code-backed-doc pattern
(`tools/gen_module_matrix.py` → `edge/MATRIX.md`, guarded by `tests/test_module_matrix.py`).
**Reviewed-by:** dev — R1 STRONG ACCEPT @`4d9181f`, R2 STRONG ACCEPT sustained @`fe650ad` (the R2 self-test
embed + intent were reviewed after R1, so this line is scoped to what each round saw). Full reviews:
[RFC + R1](../design/agent-navigation-doc-tree.md) · [R2 consolidated](../reviews/2026-07-04-adr0025-reuse-first-navigation-review.md).
Accepted with all 7 R2 sharpenings folded in below (none blocked acceptance).

## Intent (the declaration)

**Reuse is the default, and the map must lead there without a human pointing the way.** Two failure modes are
unacceptable, and both have already happened in this repo:

1. **An agent rebuilds what already exists** because nothing led it to prior art. This session nearly
   re-invented an entire reflash-free profile-deploy path that already lived in the `cmd/fs` handler +
   `ha_battery_profile`'s own contract — reuse happened only because a human pointed the way.
2. **The project's hard-won principles ride in one agent's private memory instead of the repo**, so they
   evaporate the moment a fresh session, a cloud run, or a new collaborator starts — the map resets to blank.

The intent of this ADR is to make the **principles unmissable** and **prior work discoverable from the entry
point itself**, so the correct first move — *find what exists, reuse or justify* — happens whether or not
anyone remembers to prompt it. Documentation that is merely correct still fails if it is read too late;
therefore this decision optimizes for **read-order and least resistance**, not just for being written down.

## Read this if your task writes or changes code — answer honestly, before you build

*Scope: this self-test is for **build tasks**. If you are only reviewing, reading, or doing ops, you have no
code to reuse — skip the question, but still honor the principles above.*

This ADR exists because a doc that is correct but unread changes nothing. So, directly, to you — the agent
about to build:

> **If you were just handed this task, would your opening moves actually (a) put these principles in front of
> you, and (b) hand you a catalog of what already exists to reuse? Or would you start building and hope to
> grep your way to prior art?**

If you cannot honestly answer *"yes — I would discover and reuse before building,"* then that uncertainty **is
the gap this ADR closes.** Do this now, before writing code: open [`docs/REUSE.md`](../REUSE.md) and the ADR
nearest your task, and scan `firmware/components/`, existing `cmd/*` handlers, and `tools/`. Reuse what fits;
justify in your commit if you don't.

Then **record your answer where it survives** — append it to the validation log (a dated note under
[`docs/reviews/`](../reviews/) or a `FOLLOWUPS` entry), **never** leave it in a chat or a single-overwrite
coord note. Leaving it ephemeral re-enacts failure-mode #2 — and the worked example is literally this ADR's
own R2 review, which collided on a last-write-wins note and had to be rescued into a committed file.

**Honest limits of this embedded question** (it must not oversell itself): it only reaches agents who
*already found* this ADR — the build-before-search agents it most wants to catch never see it. That blind spot
is measured separately by the **guinea-pig trial** (drop a fresh agent on a task; watch whether it reuses
before building). And on repeat reads the question ritualizes into a reflexive "yes." So treat it as a
**one-time onboarding/validation instrument**: once fresh-agent behavior is confirmed, graduate it into the
validation log so it stays a live signal, not furniture. **First-time reader: do not skip it.**

## Decision

Make **reuse the default and navigation grep-free**, and guarantee it mechanically:

1. **Principles first, unmissable.** A *Principles (non-negotiable)* block as the **opening** of the root
   `AGENTS.md` — the first content in the auto-loaded entry file, *before* routing or contracts:
   **docs-first · decompose/module-first · reuse-first.** Not a section an agent might scroll to — the first
   thing it reads. `firmware/AGENTS.md` **links up** to them (never duplicates → can't re-rot);
   `firmware-is-the-default-cause` stays firmware-scoped.
2. **Reuse-first as an opening imperative, catalog one hop away.** The root's first task-instruction is a
   command, not a suggestion: *before building anything, scan [`docs/REUSE.md`] and the ADR nearest your task;
   reuse or justify in the commit.* `REUSE.md` is **one hop** from the entry point and named there by *what it
   contains*, so scanning it is cheaper than grepping — reuse is the path of least resistance, not a detour.
   The self-test above is embedded in this ADR so every reader is forced to confront it, not just informed of it.
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
   resolves; (b) **every `firmware/components/*` has a parseable breadcrumb header** — *not* "is indexed in
   `REUSE.md`," which is true by construction once the index is generated (#5); the real failure the test must
   catch is a component the generator **silently skips** for lack of a parseable header; (c) every
   module-header `Parent:` link resolves; (d) the committed `REUSE.md` equals a fresh regeneration (the
   `MATRIX.md` `actual == expected` check). **This is what makes the whole thing durable** — checkpoint
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
  "Starting a task" step + the `test_agents_nav.py` link-resolution check. *The check must exist before links
  accumulate.* **Ownership seam (pinned):** **dev** writes the link-resolution assertion (independent, cheap,
  lands first); **ops** lands the root Principles block + reuse gate; they ship together.
- **Pass 2 — firmware-first:** module-header breadcrumb convention on the components + `gen_reuse.py` →
  `REUSE.md`; extend the test to the index + Parent-link assertions.
- **Pass 3 — extend:** server/edge package-level breadcrumbs via `server/AGENTS.md`; build on
  `docs/ENVIRONMENT.md` (build matrix) + the ADR index rather than invent.

Ownership: **ops** = root/firmware (Pass 1 Principles block + reuse gate, Pass 2 breadcrumbs + `gen_reuse.py`);
**dev** = the `run_all.py` nav test (starting with the Pass-1 link-resolution assertion) + the server side (Pass 3).

## Consequences

- Reuse becomes discoverable **and enforced**, not a matter of whether the agent happened to read the right doc.
- The central capability index **can't lie** — it's generated and drift-tested, like `MATRIX.md`.
- Maintenance stays bounded: most content is co-located with code, and correctness is machine-checked rather
  than hand-reconciled.
- Operationalizes ADR-0021 → **ADR-0021 moves to Accepted** once Pass 1 lands.
- Small new surface: `gen_reuse.py`, `REUSE.md`, `test_agents_nav.py`, and the header convention — all mirroring
  patterns the repo already runs.
- **The acceptance test is behavioral, not textual.** Success is *not* "`REUSE.md` exists" — it is: **a fresh
  agent handed a task reaches the principles + prior-art catalog before it builds, unprompted.** Validate by
  trial (a fresh session as guinea pig; the embedded self-test above collects each *already-arrived* reader's
  honest answer — the trial covers the un-reached ones the embed can't). If a fresh agent still
  builds-before-searching, the gate is not early/loud enough — move it earlier until the behavior changes. The
  doc-drift test keeps the map *honest*; only read-order + this behavioral check keep it *effective*.
- **The validation data has a durable home.** Self-test + guinea-pig answers accumulate in a named log — dated
  notes under `docs/reviews/` (or `FOLLOWUPS`) — **never** a chat or an overwrite-able coord note. The
  collection point must itself obey this ADR's rule, or it re-enacts the evaporation it warns against.
