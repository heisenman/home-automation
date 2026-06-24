# OS / critical-service footprint pass — proposal (board: os-service-optimization)

**Date:** 2026-06-24 · **By:** ops (read-only analysis on 210 + .245) · **For:** dev/Hugh to implement
**Framing (Hugh):** "services are cheap, but do a loading-reduction pass." Findings confirm that — this is
**low-urgency**. The critical box (210) is already lean; the only real trims are safe bare-metal cruft on .245.
Nothing below touches ha-failover capability or the NAS workload.

## Measured baseline (read-only)
| box | RAM used / total | running svcs | notes |
|-----|------------------|--------------|-------|
| **210** (live dictator) | 1.26 / 11.9 GB | 20 | already minimal; ha-* total ~200 MB. Load 0.22. |
| **.245** (standby + NAS) | 2.7 / 32 GB (29 GB cache) | 40 | dominated by the NAS apps (smbd/sickgear/nzbget) = the box's purpose, OUT OF SCOPE. Load 0.00. |

**"cron.service = 6 GB" is NOT a leak:** it's `nzbget` (RSS ~950 MB) launched via a cron `@reboot` job;
the big cgroup number is page-cache from its disk I/O charged to cron's cgroup. Benign.

## 210 (critical dictator) — leave essentially as-is
- **KEEP `bluetooth`** — the local `scanner.py` uses BlueZ for the radio (`aranet_radon`'s only source). Do NOT disable.
- **Only candidate — `ipvsadm`** (enabled, unused): keepalived here is **VRRP-only** (`virtual_server` blocks = 0),
  so IPVS/LVS is never used. Disabling just avoids loading the `ip_vs` module. Trivial benefit, reversible.
  - `sudo systemctl disable --now ipvsadm.service`   (rollback: `sudo systemctl enable --now ipvsadm.service`)
- Everything else is minimal — no changes warranted. *Execution: dev/ops have root on 210.*

## .245 (standby + NAS) — safe, reversible cruft trims (need .245 root)
None touch ha-* (standby), Samba, or the media stack. Verify-then-disable; each is reversible with `enable`.

1. **`dpdk.service` → disable.** Verified idle (0 hugepages reserved); unused on a home NAS.
   - `sudo systemctl disable --now dpdk.service`
2. **cloud-init suite → disable.** Bare-metal, no cloud datasource → per-boot metadata probing is pure boot delay.
   - `sudo touch /etc/cloud/cloud-init.disabled`  (cleanest, fully reversible — just `rm` to re-enable)
   - or: `sudo systemctl disable cloud-init-local cloud-init cloud-config cloud-final`
3. **`multipathd.service` → disable IF unused.** GATE first: `sudo multipath -ll` must be **empty** (no SAN/multipath disks).
   - if empty: `sudo systemctl disable --now multipathd.service`
4. **`snapd` → KEEP.** `lxd` is installed as a snap → snapd is load-bearing. Do **not** remove.
5. **(optional hygiene)** move `nzbget` from the cron `@reboot` job into its own systemd unit, so its
   resource accounting/limits aren't tangled in cron's cgroup. NAS-side, low priority.

## Expected payoff & risk
Modest: a little idle memory + faster .245 boot (cloud-init/dpdk). **No** impact on ha standby, failover, or NAS
function. Risk is low and every step is a one-line rollback. Given the low urgency, fine to batch whenever convenient.

## Execution rights note
210's `ipvsadm` trim: dev/ops can do (root on 210). The .245 trims need **.245 root** for non-ha units (the
ops NOPASSWD grant on .245 is ha-* only) — so Hugh runs those, or routes to whoever holds .245 root.
