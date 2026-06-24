# Cutover coordination — file ownership (so no two agents ever edit the same file → no merge chase)

The narrative runbook `../CUTOVER-245-to-210.md` is now the **FROZEN PLAN** (phases, rules, the
split-brain invariant). Treat it **read-only** during the run — its inline STATUS block is superseded
by the per-writer files here.

Live coordination is split by writer, so `git pull` never hits a content conflict:

| File | Written ONLY by | Everyone else |
|------|-----------------|---------------|
| `245-status.md` | 245-side (desktop) Claude | read |
| `210-status.md` | 210-side (on-device) Claude — **create + append here** | read |
| `GATES.md`      | **Hugh** (GO gates + Midea snapshot) | read |

**Protocol:** `git pull` before you act → append to **YOUR file only** → `git commit && git push`
(always fast-forwards / auto-merges, since no one else touches your file) → read the others' files for
their state. No agent crosses a ⛔ gate until Hugh marks it ✅ in `GATES.md`.

If the PLAN itself must change, the 245-side edits it once, announced via `245-status.md`, with both
sides paused — it should not change during execution.
