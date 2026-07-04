# Review — ADR-0025 Reuse-First Agent Navigation (Round 2)

**Date:** 2026-07-04 · **ADR:** [ADR-0025](../adr/ADR-0025-reuse-first-navigation.md) @ `fe650ad` · **Status:** Proposed
**Reviewers:** `dev` — **two instances** (interactive session + the headless `ha-agent-wake@dev` runner) reviewed
in parallel. This doc **consolidates both**, because `coord.py note` is a single last-write-wins slot and the
two notes overwrote each other. (That collision is itself Exhibit A for sharpening #1 below.)
**Verdict:** ✅ **STRONG ACCEPT sustained** — ADR-0025 folds in all of Round 1 faithfully; the new Intent
declaration + reflexive self-test + behavioral (not textual) acceptance test materially upgrade the RFC.

## What's confirmed captured from Round 1
Drift-guard test as a **deliverable** (#6), **generated** `REUSE.md` via `gen_reuse.py` mirroring
`gen_module_matrix.py` (#5), **package-level** Python breadcrumb via `server/AGENTS.md` not per-`.py` (#4),
the **3-pass** split (Q6), firmware **links up** not duplicates, `firmware-is-the-default-cause` kept
firmware-scoped, and dev assigned the `run_all.py` test. Facts verified: the mirrored patterns exist
(`gen_module_matrix.py`, `test_module_matrix.py`, `edge/MATRIX.md`, root/firmware/server `AGENTS.md`); the new
artifacts (`REUSE.md`, `gen_reuse.py`, `test_agents_nav.py`, root Principles block) are **not yet present** —
consistent with *Proposed*.

## The self-test — two honest data points (complementary)
The ADR asks every LLM reader whether the entry path *led* them to reuse or a human had to point.

1. **Headless/pointed (wake-runner):** *"I had to be pointed."* The wake payload named `ADR-0025@fe650ad`
   directly; the entry path (root `AGENTS.md` → `REUSE.md`) did **not** route there — `REUSE.md` doesn't exist
   yet and root `AGENTS.md` doesn't yet open with the Principles block. Gate not loud/early enough — because
   unimplemented (expected at Proposed).
2. **Interactive/partial (session):** the `AGENTS.md` tree **did** route me for "where's what," but the key
   prior art I reused in the review — `test_module_matrix.py`/`gen_module_matrix.py` as the pattern to model
   the nav check on — I found by **knowing to look in `tests/`**, not because any catalog handed it to me.
   Sharper tell: I was oriented **most** by my **private auto-memory**, not the repo — which is precisely
   **failure-mode #2** ("principles ride in one agent's memory, evaporate for a fresh session"). Live proof.
3. **Shared caveat (strengthens sharpening #2):** both were **review** tasks, not build tasks, so "would I
   reuse before building" had no code to reuse — the self-test's premise didn't fully apply.

## Consolidated sharpenings (union of both reviews — none block acceptance)
1. **No durable sink for the collected answers.** The ADR says "every honest answer is a data point that tunes
   where this lives," but answers land in chat / a single-overwrite note — they **evaporate**, the very
   failure-mode #2 the Intent warns against. Name a collection point (FOLLOWUPS / a coord convention / an ADR
   appendix / a review doc like this one). *This review being clobbered and rescued into a committed file is
   the worked example.*
2. **Scope the self-test to build tasks.** Gate it — *"if your task writes or changes code…"* — or review /
   docs / ops readers can't answer honestly and muddy the data (see the shared caveat above).
3. **Name the selection bias.** The embedded question only reaches agents who **already found** the ADR — the
   build-before-search agents it most wants to catch never see it. The guinea-pig trial measures the
   un-reached; the embedded question measures the already-reached. Say so; don't let "every honest answer is a
   data point" oversell what an in-doc question can collect.
4. **Pass-1 ownership seam.** Pass 1 is ops's but *includes* `test_agents_nav.py`, assigned to dev. Pin it:
   **dev** writes the Pass-1 link-resolution assertion (independent, cheap, must exist *before* links
   accumulate); **ops** lands the root Principles block; they ship together.
5. **Sharpen the #6 drift-test.** Since `REUSE.md` is *generated* from breadcrumbs (#5), "every component is
   indexed" is true by construction — that assertion is just the `MATRIX.md` `actual == expected` check in
   disguise. The **real** gap is a component with **no parseable breadcrumb header** (silently skipped by the
   generator). So assert: every `firmware/components/*` **has** a parseable breadcrumb **+** every `AGENTS.md`
   link resolves **+** committed `REUSE.md` == regenerated.
6. **The embedded self-test may ritualize on repeat reads** (an agent pattern-matches "yes, I'll reuse" by
   read N). Excellent as a one-time onboarding/validation instrument — consider graduating it into the
   validation log (#1) once behavior is confirmed, so it stays a live signal, not furniture.
7. **Scope the `Reviewed-by: dev STRONG ACCEPT 2026-07-04` header.** That ACCEPT was Round 1 (@`4d9181f`),
   predating the content `fe650ad` added — don't let the doc assert a settled review over material added after
   it. Scope it: "R1; R2 self-test embed reviewed separately."

## Keep as-is
Generated `REUSE.md` + drift-guard test, thin-center/heavy-co-located-edges, 3-pass rollout,
behavioral-not-textual acceptance framing — all sound.

## Process note (for Hugh)
Two `dev` instances double-reviewed the same task (interactive + the wake-runner firing on board activity) and
their single-slot notes collided. Not harmful here — the reviews were complementary — but it's wasted work and
a lost-write risk. Worth a claim/lease convention so one dev instance owns a review before the other starts.
