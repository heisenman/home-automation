# Failover SSH decouple (ADR-0033 `failover-ssh-decouple`)

Goal: the air-gap boundary (`.210 wlp2s0`) should carry **no general SSH**. The failover machinery used SSH
for two very different things; we split them.

## The two channels (mapped 2026-07-10)
| Channel | What | Decouple |
|---|---|---|
| **Control — fencing** | `notify.sh` MASTER runs `ssh peer "sudo systemctl stop <controller>"` | → **cluster bus** (signed fence over `ha/cluster/#`); no SSH |
| **Data — reconcile/sync/provision** | bulk `ssh`+`scp`: sqlite `.backup`, parquet, secrets, remote python | → **dedicated port 47222** + forced-command-locked key |

**Security finding:** `id_cluster` has **no forced-command** — a full interactive shell that also runs `sudo`
on the peer. That shared key is effectively a root capability across .210/.245/ha-2. The forced-command lock
shrinks it to a single reconcile verb.

## Status
**Part 1 — DONE (commit dbb9d29):**
- Dedicated failover SSH line on **port 47222** (`sshd-failover-port.conf`, additive — 22 stays for household
  admin). Up on `.210` + ha-2, verified.
- Failover scripts parameterized: `CLUSTER_SSH_PORT` (default 22 = household pair unchanged; the air-gap pair
  sets `CLUSTER_SSH_PORT=47222` in `~/ha-airgap-standby/failover/cluster.env`).

**Part 2 — IN PROGRESS (careful — touches the failover safety net):**
1. **Fence → cluster bus** — signed fence command on `ha/cluster/fence`; a `ha-fence-listener` on each box
   validates (HMAC + freshness) and stops its own controller. Removes SSH from the control path. **PREREQ to
   closing 22.**
2. **Forced-command lock** on `id_cluster` — restrict the key to the reconcile wrapper only (needs reconcile's
   inline `python -c`/`bash` calls routed through fixed entrypoints).
3. **Firewall cutover** — `airgap.nft`: drop 22 on `wlp2s0`, allow only 47222 from ha-2. **Last**, drill-verified.
