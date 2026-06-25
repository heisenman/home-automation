# Failover runbook — operate / test / promote

See `README.md` for the architecture + Core Rule (primary supremacy). This is the operational procedure.
**Runtime is LLM-free + GitHub-free** — everything below is keepalived/systemd/bash on the local LAN.

## Per-box setup (one-time)
On EACH box (`visko`):
```bash
cd ~/home_automation
cp failover/cluster.env.example instance/cluster.env
# edit instance/cluster.env:  210 -> ROLE=primary PEER_HOST=192.168.0.245
#                             245 -> ROLE=standby PEER_HOST=192.168.0.210
sudo apt install -y keepalived            # needs the box's sudo password
./failover/deploy.sh                       # renders config, installs units (starts nothing)
```

## Go-live (supervised, ordered)
1. **Primary (210) first** — it's already the live dictator; keepalived just takes the VIP:
   ```bash
   sudo systemctl enable --now keepalived
   ip addr show | grep 192.168.0.200        # VIP should land on 210
   ```
2. **Standby (245) second:**
   ```bash
   sudo systemctl enable --now keepalived
   sudo systemctl enable --now ha-primary-watch.service ha-standby-sync.timer
   ```
   Confirm 245 stays BACKUP (no VIP) and its `ha-controller` stays **inactive** while 210 is healthy.
3. Verify exactly one controller (210) — `tail -f /var/log/ha-failover.log` on both.

## Controlled failover TEST (do once, supervised)
Simulate primary death and confirm clean takeover + auto-demote:
```bash
# on 210: simulate failure
sudo systemctl stop keepalived          # (or: stop ha-api / pull the cable)
# within ~3-5s, on 245:  VIP appears, notify MASTER fences 210 + starts 245's controller
ip addr show | grep 192.168.0.200       # now on 245
journalctl -u ha-controller -n 5        # 245 making Midea decisions
# >>> verify EXACTLY ONE controller active (245); 210's is stopped (fenced) <<<

# failback (primary supremacy, automatic):
# on 210: recover
sudo systemctl start keepalived         # 210 reclaims VIP after preempt_delay (30s)
# 245 auto-demotes: keepalived BACKUP notify stops its controller, AND primary-watch enforces it.
# verify: VIP back on 210, 210 controller active, 245 controller inactive.
```
Expected: **never two active controllers** at the same instant (the stop-before-start fence + the
zero-gap-is-OK invariant). If you ever see both active, STOP and disable keepalived on 245.

## Promote 245 PERMANENTLY (210 dead & being replaced) — user-permissioned ONLY
Auto-failover never makes 245 permanent; to deliberately re-designate:
```bash
# on 245: edit instance/cluster.env -> ROLE=primary, PEER_HOST=<new peer or blank>
./failover/deploy.sh                     # re-renders as MASTER/150
sudo systemctl disable --now ha-primary-watch.service   # stop yielding to the (dead) old primary
sudo systemctl restart keepalived
# (when a replacement box joins, set IT to ROLE=standby pointing at 245)
```

## Monitoring
- **`./failover/cluster-doctor.sh`** — read-only invariant + capability check (exactly-one-controller,
  one VIP holder, dictator coherent, fresh heartbeats, SSH/keepalived/sqlite3/VIP-reach). Run on demand and
  **after every failover** + before promoting/joining a box. Exit 0 = healthy, 1 = a FAIL. Makes no changes.
- `/var/log/ha-failover.log` on both boxes (notify / primary-watch / sync events).
- `GET http://<box>:8123/cluster/status` (once the cluster-RPC lands) — role / controller / health.
- `journalctl -u keepalived` for VRRP state transitions.

## Disable / rollback the whole thing
```bash
sudo systemctl disable --now keepalived ha-primary-watch.service ha-standby-sync.timer
# controllers revert to manual (210 stays dictator via its own enabled ha-controller).
```

## Refinements (2026-06-24, post-go-live)
Three hardening tweaks applied after the live test. None change the Core Rule; all are belt-and-suspenders.
1. **Startup-transient suppression** (`notify.sh`): a keepalived (re)start fires `BACKUP` then `MASTER`
   ~1-3s later, which used to blip the controller off→on. BACKUP now waits `BACKUP_GRACE` (4s) and only
   stops the controller if the VIP is **not** held — a transient (we won MASTER) leaves the controller up.
   `FAULT` still stops immediately (genuinely unfit).
2. **MQTT cross-check** (`primary-watch.sh`): the standby's yield trigger now confirms the primary is back
   via SSH **or** a fresh retained heartbeat read straight from the primary's broker
   (`ha/cluster/$PRIMARY_NODE/heartbeat`, anon). A `ts` within `HEARTBEAT_FRESH` (12s) is required —
   retained heartbeats outlive a dead node, so stale ones are ignored. Two independent channels → more
   reliable detection of the primary's return; SSH alone still works if MQTT is unavailable.
3. **Broker bridge** (`failover/mosquitto/cluster-bridge.conf`): mirrors only `ha/cluster/#` between the
   two brokers so each box's local broker carries both heartbeats. Install on `.245` only (it bridges
   outbound to 210's anon broker — no creds): `sudo cp … /etc/mosquitto/conf.d/ && sudo systemctl restart mosquitto`.
   Also run the standby's own publisher: `sudo systemctl enable --now ha-cluster-heartbeat`.
   Rollback: `sudo rm /etc/mosquitto/conf.d/cluster-bridge.conf && sudo systemctl restart mosquitto`.

## Phase-B relay coordinator binding (2026-06-24)
`notify.sh` also binds **`ha-relay-coordinator`** (ADR-0015 Phase B) to the VRRP role, same one-writer
invariant as the controller — **only the dictator signs+publishes edge relay allowlists**:
- **MASTER** → (re)start it *after* the `ha-edge-mapper` recompute, so it re-evaluates coverage from the new
  dictator's own reach. (`restart` starts it if the VIP-guard had parked it on the old standby.)
- **BACKUP / FAULT** → stop it (a standby must never publish).
Best-effort + non-blocking (never holds up the VRRP transition); a clean noop on a box where the unit
isn't installed. The unit is **also** `HA_VIP`-guarded as an independent backstop, so a missed notify still
can't make the standby publish. Unit name overridable via `RELAY_COORD_UNIT` in `cluster.env`.
Covered by sudoers (`systemctl ha-*`). Rollback for the whole Phase-B publish path is unchanged
(`systemctl stop ha-relay-coordinator` + `mosquitto_pub -r -n -t home/edge/<node>/relay`).

## Bringing up a new peer → record-keeping elevation (ADR-0018)
A new (or rebuilt) box becomes a *trustworthy record-keeper* only after it holds the full
**config-of-record + data-of-record (hot tier AND parquet archive)** — not just after it has run a while.
This is a gated, idempotent, one-command step. **It is part of provisioning, not an afterthought** — the
2026-06-25 incident was exactly this step being skipped (210 promoted to dictator with ~1.5 d of archive
while `.245` held since January; the dashboard silently served truncated history).

1. Sync config + data and assert eligibility, FROM an existing record-keeper (the current dictator, or
   whoever holds the deepest archive):
   ```
   failover/provision-peer.sh --from <source-host>             # full: config + hot + archive + HARD GATE
   failover/provision-peer.sh --from <source-host> --data-only # when THIS box's config is already authoritative
   ```
   Stages: config-of-record (`sync-standby`) → hot (`reconcile-history --once`) → archive
   (`reconcile-parquet --once`, **row-level** keyed `device_id,ts,metric`) → **HARD GATE** (archive parity
   vs source). Exit 0 = *record-keeping eligible*; exit 1 = **not trusted as dictator-of-record**.
2. Confirm cluster-wide: `failover/cluster-doctor.sh` — the **Archive completeness (ADR-0018)** check must
   PASS (both boxes' parquet archives converged) alongside config-completeness + hot convergence.
3. The archive merge is **bidirectional and lossless**: each box keeps the rows the other lacks (the union),
   so running it also backfills the *source* with anything unique to the new box. Re-running is a safe no-op.

*One-off precedent (2026-06-25):* `provision-peer.sh --from 192.168.0.245 --data-only --yes` on 210 (already
dictator, config authoritative, archive thin) → both boxes converged to 8,777,107 rows, earliest 2026-01-07,
gate PASS. The demote/promote step was omitted (210 was already the live dictator).

*Ongoing:* `reconcile-parquet.sh --loop` (VIP-gated, slow cadence) keeps archives convergent between
provisionings; parquet only diverges when a swap straddles a daily compaction boundary.

## Notes / still-TODO before go-live
- **Cluster-RPC** (`/cluster/status|demote|claim` + MQTT heartbeat) is the 210-side code task; until it
  lands, fencing/health use SSH `systemctl is-active`/`stop` (already wired in the scripts).
- Confirm `notify.sh`'s `sudo systemctl stop/start ha-controller` is covered by each box's sudoers
  (NOPASSWD `systemctl ha-*`). `.245` already covers stop; verify start + the standby's unit are allowed.
- Optional hardening: restrict the cluster SSH key with a forced-command in `authorized_keys`.
