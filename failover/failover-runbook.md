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

## Notes / still-TODO before go-live
- **Cluster-RPC** (`/cluster/status|demote|claim` + MQTT heartbeat) is the 210-side code task; until it
  lands, fencing/health use SSH `systemctl is-active`/`stop` (already wired in the scripts).
- Confirm `notify.sh`'s `sudo systemctl stop/start ha-controller` is covered by each box's sudoers
  (NOPASSWD `systemctl ha-*`). `.245` already covers stop; verify start + the standby's unit are allowed.
- Optional hardening: restrict the cluster SSH key with a forced-command in `authorized_keys`.
