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

## STATUS 2026-07-10: SSH-decouple COMPLETE. The air-gap boundary carries NO general SSH.
- **Control path** (fence) → cluster bus, LIVE + acting for the air-gap pair (ha-2 ⇄ .210). No SSH.
- **`tcp/22` on `wlp2s0`** → CLOSED.
- **Data path** → dedicated `tcp/47222` (from ha-2 only), **ForceCommand-locked to reconcile-only** — not a
  full shell. Port 22 stays a full-shell management escape hatch.

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
2. **Forced-command lock on port 47222 — DONE 2026-07-10.** `cluster-ssh/reconcile-agent.sh` is a
   `ForceCommand` wrapper wired via `Match LocalPort 47222` (installed by `provisioning/airgap/reconcile-lock/
   install-reconcile-lock.sh` on both boxes). It restricts port-47222 SSH to an EXACT-shape allowlist of the
   reconcile verbs (`reconcile-history --export/--merge`, `reconcile-parquet --list/--merge`, the
   `rebuild_parquet_manifest.py` rebuild, `sqlite3 … .backup`, `test -f`, `rm -f /tmp/…`) plus their scp
   transfers; data fields are charset-constrained (no shell metacharacters) and `..` is hard-refused (no
   traversal/exfil). Everything else → refused. **Mechanism, not authorized_keys `command=`:** port-scoped so
   `id_cluster` stays usable for management on :22, and the wrapper covers ANY key on 47222. **scp needs `-O`**
   (OpenSSH 10 defaults to the SFTP subsystem, which `ForceCommand` would break) — the reconcile `SCP()`
   wrappers pass `-O` for the legacy `scp -t/-f` server command. Verified: full history+parquet reconcile moves
   data through the lock; `ssh -p47222 <box> id` refused; :22 full-shell intact.
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
- **Unlock port 47222** (if reconcile breaks / need a full shell there): remove the terminal `Match LocalPort
  47222` block from `/etc/ssh/sshd_config` (a timestamped `sshd_config.bak.*` is written on install),
  `sudo sshd -t && sudo systemctl reload ssh`. Port 22 is always the full-shell escape hatch regardless.
