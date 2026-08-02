# RESUME — 2026-08-02: SGP41 adopted, and two "it needed a human" bugs closed

**Seat:** dev2 (interactive with Hugh). **Tree:** clean, `HEAD == origin/main`.
**Prior session:** [RESUME-2026-08-01b-sgp41-and-board-sweep.md](RESUME-2026-08-01b-sgp41-and-board-sweep.md).

The session started as "why won't my SGP41 adopt" and became a run at a single theme Hugh named directly:

> **If this requires your intervention, the structure is wrong.**

Everything below is that principle applied. Both bugs were *reported by the system in confident, coherent
prose* — and both statements were wrong in the same way: they described a condition the system had not
actually established.

---

## 1. The SGP41 wouldn't adopt — and prod said the board was a relay

Hugh reflashed a board as `sgp41_mech`. It came up correctly, announced `abilities=["sgp41_gas"]`, and prod
intake answered:

> node 'sgp41_mech' announced no gas ability (['sgp41_gas']) and has no registry record. A relay-only node
> needs no device record … Nothing to adopt into a room.

**Nothing was wrong with the board.** ha-2 ran the pre-`7c65a9f` `control.py`, whose `_GAS_CAPABILITIES` has
four entries and no `sgp41_gas`. The lookup missed and fell through to the relay-only branch — which is
phrased as a verdict about the *hardware*, and even echoes back the ability it just failed to match.

`7c65a9f` had reached ha-2 **in halves**: the banding half (`gas_compensation.py`, `viewmodel.py`) went out
08-01 and diffed 0 lines; the intake/flash/backfill half never did. Six files deployed under a VIP-inhibit
window, checksum-verified, VIP `.1.200` held. Details + the ghost-hello gotcha:
[airgap/HA2-DEPLOY-PENDING.md](airgap/HA2-DEPLOY-PENDING.md). Commit `cb1d325`.

> **Verify per-file, not per-commit.** A merged commit can be live on ha-2 in part.

## 2. Then it dead-ended again — on a decision the system could have made itself

> device_id 'gas_mech_closet' is already taken … **Register this node manually with a distinct device_id.**

`mech_closet` already held a live BME680. Auto-registration derives `device_id` as `gas_<area>` — which
silently assumes **one gas sensor per room**, while redundant/complementary sensing in a room is a stated
design goal. Collision is expected input, and the code treated it as a wall, telling an operator to
hand-edit prod YAML.

I offered Hugh a menu of naming conventions. That was the wrong move, and his one-line reply above is why.

Three defects in one function, each forcing a human (`2376bfe`):

| # | defect | fix |
|---|---|---|
| 1 | `gas_<area>` assumed one gas sensor per room | `_gas_device_id()` qualifies by family → `gas_mech_closet_sgp41`, numeric tail beyond that. Incumbent untouched → **no history migration**. Safe because nothing parses area back out of a device_id. |
| 2 | `dry_run` returned **before** reading the registry | Registry read first. Preview and apply now provably agree (test). A preview that can't see the state it previews against is worse than none. |
| 3 | unknown ability → the relay-only verdict (bug #1) | Told apart. The drift case names the **server**, says the node is fine, and says **do not re-flash**. |

The function had **zero tests** — a large part of why it shipped three ways to need a human. Added 10.

**Hugh confirmed: "SGP41 working well."**

## 3. The compactor had been half-finishing on every box

Noticed incidentally while checking ha-2's units. `ha-compactor` failed on **both** boxes at 02:00, the
first failure ever:

```
File "server/storage/compactor.py", line 228, in compact
  from server.storage import latest_cache
ModuleNotFoundError: No module named 'server'
```

`ExecStart` runs the file **by path**, so `sys.path[0]` is `server/storage/`, not the repo root. It imports
fine under pytest, under `-m`, and from a REPL — **only systemd breaks**. `compactor.py` was the *only*
script-path entrypoint in `systemd/` without a `sys.path` bootstrap; every sibling had one. It had never
needed one, until `c073de8` added a `server.` import.

Two orderings made it worse than a crash:

* **The import was deferred inside `compact()`**, so it blew *after* the `DELETE` committed — rows pruned,
  then `latest_readings.prune` **and** the WAL checkpoint skipped, reported as a crash. Hoisted to module
  scope: *an import that can fail must fail before the destructive step.*
* **Compaction not sticking is not local.** ha-2 pruned 899,612 rows at 02:00 and had **452,128 back within
  45 minutes** — `reconcile-history` merges a peer's rows with `INSERT OR IGNORE`, and the air-gap standby
  still held them. **A compactor broken on one peer makes the other's compaction futile.** The standby was
  **10 minutes from its own first failure** (it had `c073de8` and no bootstrap).

**Guard — the class, not the instance.** A test walks every `systemd/*.service`; any unit running a repo
`.py` by path whose script imports `server.` without a bootstrap fails the suite. It would have caught
`c073de8` on landing. A second runs the compactor end-to-end exactly as systemd does (subprocess, script
path, repo cwd). **Both confirmed to fail against the pre-fix file**, not assumed. Commit `7d3f74f`.

Also corrected `_cutoff_ts`, named and documented "yesterday midnight" while computing **today** midnight
via a `- timedelta(days=0)` no-op. Behaviour unchanged and deliberate; the description cost real time.

**Verified after a full reconcile cycle** (the number that mattered — this morning it went 0 → 452,128 in
the same window):

| stack | 02:51 | 03:10 | pre-cutoff rows |
|---|---|---|---|
| .210 | 8,510 | 9,897 | **0 → 0** |
| standby | 49,357 | 59,233 | **0 → 0** |
| ha-2 | 53,044 | 59,348 | **0 → 0** |

The standby's own 03:00 timer fired on the fixed code and returned `success`. ha-2's cache verifies clean
(103 pairs) *after* the merge. `.210` needed an explicit `rebuild_latest_readings --rebuild`: its rows were
already gone, so `compact()` returns early and never reaches the prune — its 26 stale entries would have
sat there indefinitely.

## 4. Idle power re-measured — the open question closes

Answered RESUME-08-01b item #4. Idle **4.45 W / 98.2 % C2**, at or below the filed ~5 W floor; wall meter
agrees (8.35 W idle vs 9.26 W active). **CAL 178/s idle vs 199/s filed vs 472/s session-active** — it was
the session. The 07-27 keepalived window is plainly visible at 12.09 W / 88.7 % C2, and is what drags the
campaign-wide idle mean to 5.92 W. **Hugh: re-check in a month or two.**

---

## Commits

| commit | what |
|---|---|
| `cb1d325` | docs: SGP41 intake surface deployed to ha-2 (the stale-deploy write-up) |
| `2376bfe` | fix(intake): auto-registration must never hand the operator a decision it can make itself |
| `7d3f74f` | fix(compactor): repo root on sys.path — daily compaction had been half-finishing on every box |

Suite: **586 pass**, same **3 pre-existing failures** all session (firmware breadcrumbs `bme680`/`ha_eth`/
`ha_led`/`ha_wifi`; `test_rooms::test_empty_inputs_do_not_error`; `test_reconcile_merge`, environment-driven).

## Open / next

1. **Break-glass recovery key still on `.210`** — `/etc/ha-break-glass/recovery.key.MOVE-OFFLINE.pem`, open
   since 07-10. **Only Hugh can move it offline.** (Carried from the last two sessions.)
2. **The adopt panel's name box** sets a *display-name overlay*, not the device_id, and doesn't say so — it
   is what made the collision confusing. Deliberate design (a real id rename triggers a fleet restart), but
   the label invites the misreading. Unowned, cosmetic.
3. **NOx bands still PROVISIONAL** — no NOx history to shadow-tune against. Re-tune after a few weeks
   (ADR-0035, `SENSOR-METHODOLOGY.md` §3.2b). `sgp41_mech` is now feeding real data toward that.
4. **Idle power re-check in a month or two** (Hugh's call). Baseline to beat: 4.45 W / 98.2 % C2 / 178 CAL/s.
5. `ha-service-healer` on the air-gap standby — still deliberately excluded, an open decision, wants a drill.
6. Board: `d1001-graph-p2c/d/e` chain, `standby-c6-ota-intake` (LOW/deferred).

## The lesson worth carrying

Both bugs **explained themselves confidently and wrongly**, and both explanations pointed at the wrong
actor — the hardware, and the operator. When a message tells a person to go do something by hand, that is
the smell. Ask why the system could not resolve it, and fix *that*. Reserve the question for genuine
business choices; derive everything else. And when the system genuinely cannot know, name the actor and say
what **not** to do ("this server is behind the node — deploy and re-adopt, do not re-flash").
