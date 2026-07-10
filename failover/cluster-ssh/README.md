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

## STATUS 2026-07-10: control-path SSH is OFF the air-gap boundary — `tcp/22` on `wlp2s0` is CLOSED.
Fence-over-bus is LIVE and acting for the air-gap pair (ha-2 ⇄ .210). The data path moved to `47222`.
Remaining: the forced-command lock on `id_cluster` / `47222` (item 2 below) is the last piece.

**Part 2 —**
1. **Fence → cluster bus — DONE + LIVE 2026-07-10.** Signed fence on `ha/cluster/fence`; the
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
3. **Firewall cutover — DONE 2026-07-10.** `airgap.nft`: `tcp/22` on `wlp2s0` DROPPED; `tcp/47222 from ha-2`
   allowed (dedicated data/reconcile port). Applied via `apply-airgap-firewall.sh apply` (180s auto-rollback
   dead-man) then `commit`. Verified in-window: ha-2→.210:22 CLOSED, :47222 OPEN, cluster bus + VRRP intact,
   ha-2 bus-fence reaches the .210 acting listener, reconcile still moves data over 47222.

## Air-gap deployment (2026-07-10, box-local — repo stays prod-pure)
- **.210 air-gap listener:** `ha-ag-fence-listener.service` (box-local unit in `~/ha-airgap-standby/failover/systemd/`),
  reuses the repo `fence.py`, `FENCE_HOST=210-airgap`, broker `.1.200`, **ACTING** (drop-in
  `…/ha-ag-fence-listener.service.d/acting.conf` sets `FENCE_DRY_RUN=0`). Stops `ha-ag-controller`.
- **ha-2 listener:** repo `ha-fence-listener.service` + `fence.py` scp'd (ha-2 is air-gapped — no git),
  `FENCE_HOST=ha-2`, broker `.1.200`, **dry-run** (flip to acting when 47222 gets the forced-command lock).
- **notify.sh (both boxes):** `bus_fence` added + `FENCE_MODE` gate. ha-2=`bus`; .210 air-gap=`both`.
- **cluster.env FENCE vars** on each; **`CLUSTER_SSH_PORT=47222`** set on the .210 air-gap instance and its
  `RSH/SCP/peer_ssh` wrappers patched to honor it (the deployed checkout predated the parameterization).

## Recovery / rollback (if a fence or the cut misbehaves)
- **Re-open 22 on wlp2s0:** edit `airgap.nft` line back to `tcp dport 22`, `apply-airgap-firewall.sh apply`
  then `commit` (or `rollback` to drop the table; the 180s dead-man auto-reverts an un-committed apply).
- **Disarm a listener (back to observe-only):** `rm /etc/systemd/system/ha-ag-fence-listener.service.d/acting.conf;
  systemctl daemon-reload; systemctl restart ha-ag-fence-listener` → dry-run.
- **Restore ssh fence:** set `FENCE_MODE=ssh` (or `both`) in the box's `cluster.env` + revert `CLUSTER_SSH_PORT`.
- **ha-2 notify.sh** pre-bus-fence backup: `/tmp/ha2-notify.sh.pre-busfence.bak` on ha-2.
