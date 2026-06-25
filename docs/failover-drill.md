# Failover drill — runbook (ROADMAP theme A, phase A3)

A **reversible, scripted** exercise that induces a real dictator→standby failover, asserts the
single-dictator invariant holds across the swap, then fails back — capturing transition timings so we
know our actual RTO. The drill does **not** implement failover (keepalived + `notify.sh` +
`primary-watch.sh` already do); it **orchestrates and observes** one.

**Script:** `failover/failover-drill.sh` · **Preflight uses:** `failover/cluster-doctor.sh`

> **Why this is gated.** A live run briefly removes control from the current dictator and makes the
> **standby** the controller. On the current 210↔245 pair the standby is **`.245`, Hugh's fileserver** —
> so a live run makes the fileserver transiently the controller. Per ops + Hugh: a live drill (and
> *especially* actuating the Midea from `.245`) needs **Hugh's explicit OK + a maintenance window**.
> `.245` is also a **temporary** stand-in — the real standby is the future dedicated box (ROADMAP A/F),
> so we run only a *lightweight mechanism-proving* drill here, not deep `.245`-specific tuning.

## Modes
| Invocation | Effect |
|---|---|
| `./failover-drill.sh` (default `--dry-run`) | **READ-ONLY.** Runs cluster-doctor, verifies the standby holds the full critical file set, checks heartbeats, prints the exact plan + rollback. No changes. Safe anytime, incl. on the live dictator. |
| `HA_DRILL_CONFIRM=I-UNDERSTAND ./failover-drill.sh --run` | **LIVE.** Induces the failover and fails back (see phases). Refuses without the confirm token. Trap-protected. |
| `… --run --actuate` | Also prove the new dictator can actuate the Midea. **Most gated** (actuating from `.245`). |

## Prerequisites (all currently GREEN on 210↔245, verified 2026-06-25)
- `instance/cluster.env` on both boxes (`ROLE`, `PEER_HOST`, `VIP`).
- Cluster SSH key `~/.ssh/id_cluster` working **both ways** (fence/sync path).
- `cluster-doctor.sh` → HEALTHY (0 FAIL). The standby must show **"has all N critical dictator files"**
  (else it would seize control but not actuate — the exact 2026-06-24 gap, now asserted).
- Run the drill from a box with the cluster key (e.g. the primary). The script auto-detects "self" and
  acts locally vs. over SSH.

## Live phases (what `--run` does)
1. **Baseline** — record VIP holder, controller node, doctor snapshot.
2. **Induce** — `systemctl stop keepalived` on the current MASTER → VRRP fails over; the standby promotes
   (`notify.sh MASTER`: fences the old controller, starts its own, remounts `ha-api` on the VIP).
3. **Observe** (≤ `DRILL_TIMEOUT`, default 45s) — wait for VIP **and** controller to land on the standby;
   assert exactly one dictator (old master's controller fenced — no split-brain).
4. **Actuate** (`--actuate` only) — issue a gated Midea command from the new dictator + confirm ack.
   *Currently a manual step:* run `tools/device_smoke_test.py` against the standby's `ha-api` this window.
5. **Fail back** — `systemctl start keepalived` on the old master → it preempts, reclaims VIP+controller;
   `primary-watch.sh` auto-demotes the standby. Assert back to baseline.
6. **Verify + timings** — cluster-doctor HEALTHY again; print measured **RTO** (VIP + controller seize,
   and failback/demote times).

## Safety / rollback
- A `trap` restarts `keepalived` on **both** boxes on **any** exit (incl. error/abort) — an interrupted
  drill cannot leave the cluster headless.
- Dry-run is the default; the live path refuses without `HA_DRILL_CONFIRM=I-UNDERSTAND` **and** a green
  preflight (0 FAIL).
- End-to-end reversible: the only state changes are `keepalived` stop/start; controllers follow VRRP.

## Open follow-ups
- Wire the `--actuate` proof into the script (currently manual via `device_smoke_test.py`).
- **RTO budget = 600s** (Hugh 2026-06-25, 10 min). The drill now PASS/FAILs the measured failover time
  against `RTO_BUDGET_S` (default 600). RTO is an *outcome* of the VRRP/heartbeat timings, so it's the
  **budget** that's configurable, not a raw timer. **Future (theme C / add-device-flow):** per-actuator
  **`max_control_outage_s`** trait, editable in the PWA device flow → cluster budget = strictest (min)
  across actuators. With one thermal load today, 600s.
- hot.db divergence is RPO (data-loss), distinct from RTO; cluster-doctor shows 0d — non-issue now.
- Decide whether the 30-min `sync-standby` cadence is tight enough once a stricter actuator exists.
