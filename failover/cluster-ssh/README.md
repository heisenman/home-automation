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

**Part 2 —**
1. **Fence → cluster bus — DONE + LIVE (dry-run) 2026-07-10.** Signed fence on `ha/cluster/fence`; the
   `ha-fence-listener` unit on each box validates (HMAC + freshness + this-host-target) and stops its OWN
   controller. `fence.py` uses `mosquitto_pub/sub` (NOT paho) so the safety-critical listener never rides the
   shared venv. Wired into `notify.sh` MASTER via `FENCE_MODE` (`bus`|`ssh`|`both`; **default `both`** = the
   bus fence AND the legacy SSH fence, so fencing capability is never lost during cutover). Listener deployed
   on `.210` in **DRY-RUN** (`FENCE_DRY_RUN=1`) — it logs `VALID fence … [DRY-RUN]` but stops nothing.
   E2E-proven on the live bus: valid→acts(dry), wrong-target→ignored, forged/stale→rejected. **PREREQ to
   closing 22 — but the SSH fence stays until the drill flips `FENCE_MODE=bus`.**
   - Remaining rollout (drill-gated): deploy the listener on `.245` + ha-2, set `PEER_FENCE_HOST` in each
     box's `cluster.env`, drill-verify a bus fence actually stops the peer, THEN flip `FENCE_DRY_RUN=0` +
     `FENCE_MODE=bus`.
2. **Forced-command lock on `id_cluster` — SCOPED, drill-gated.** The data path (`reconcile-history.sh`,
   `reconcile-parquet.sh`, `sync-standby.sh`, `provision-peer.sh`) runs varied remote verbs over `RSH`,
   including a **dynamic `venv/bin/python3 -c '$pyexpr'`**, `bash failover/reconcile-*.sh --export/--merge/--list`,
   `sqlite3 … .backup`, `test -f`, `rm -f /tmp/…`, and bidirectional `scp`. A forced-command can't whitelist
   arbitrary python, so the design is: route **every** reconcile remote op through ONE fixed dispatcher
   (`cluster-ssh/reconcile-agent.sh <verb> [args]`, verbs = `export|merge|list|backup|rebuild|pull`), replace
   the inline `pyexpr` with a fixed script, then pin `id_cluster` to
   `command="…/reconcile-agent.sh",no-pty,no-agent-forwarding,no-port-forwarding`. Touches the LIVE reconcile
   timer path — do it with a peer to test against + a window (rides `failover-drill`).
3. **Firewall cutover — drill-gated (LAST).** `airgap.nft`: drop 22 on `wlp2s0`, allow only 47222 from ha-2.
   Only after (1) is flipped to `bus` and (2) is locked + verified.
