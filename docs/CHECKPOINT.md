# Checkpoint routine

A "checkpoint" (Hugh says "checkpoint", or a task-set / work session ends) is **NOT just a commit.** It
must leave the repo's action-item docs **true**, so the next session — human or LLM — can trust them.

The failure this guards against (2026-06-23): code shipped (edge firmware reached `v9-bankts` with the
full lockdown + OTA-hash-verify) while `FOLLOWUPS.md` still listed those as pending at `v4`. Checkpoints
were capturing code but not the wrap-up, so the action-item list rotted.

## Do ALL of these, in order

1. **Land the work.** Commit, then **push** (auto-push directive). Tests / balance-checks green; report
   pass/fail honestly.

2. **Reconcile action-item docs to REALITY — verify, don't trust intent.** Check claims against the live
   system + source, not against what we *meant* to do:
   - **FOLLOWUPS.md** — move every now-done item out of "open"; **never leave a "pending" that has
     shipped.** Keep a dated, authoritative "CURRENT OPEN" list at the top.
   - **ADR `Status:` lines** — flip `Proposed → Accepted/Implemented` when the thing is built/live; note
     what's still deferred.
   - If a doc says X is pending, **confirm X is actually pending** before leaving it (the v4/v9 lesson).

3. **Update memory.** The RESUME pointer + any state memories reflect current reality — versions, what's
   live, and the single next step.

4. **State the wrap-up explicitly** to Hugh: what shipped + verified, what's actually open, and the ONE
   resume point.

## Smell tests — if any is true, the checkpoint is NOT done

- An action-item doc lists something pending that's actually shipped / flashed / deployed.
- An ADR says "Proposed" for something running in production.
- The memory RESUME points at a step already completed.
- "Done" was claimed without checking the live system or source.
