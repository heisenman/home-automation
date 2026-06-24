# OS / critical-service footprint pass — proposal (board: os-service-optimization)

**Date:** 2026-06-24 · **By:** ops (read-only analysis on 210 + .245) · **For:** dev/Hugh to implement
**Framing (Hugh):** "services are cheap, but do a loading-reduction pass." Findings confirm that — this is
**low-urgency**. The critical box (210) is already lean, so there's little to do today; the pass is mostly a
forward-looking lean-base profile for 210 and the **future dedicated HA box**.

> **⚠️ Scope guardrail (Hugh, 2026-06-24):** **.245 (superbuddynas) is a critical fileserver, NOT a dev/optimization
> target — ever.** It's a *temporary* HA stand-in until a dedicated second box is bought. Touch ONLY its `ha-*`
> guest services; **never** reconfigure the host. The .245 service-trim ideas from the first draft are **RETRACTED**
> (kept below only as a reference profile for the future dedicated box, explicitly NOT a recommendation to touch .245).

## Measured baseline (read-only)
| box | RAM used / total | running svcs | notes |
|-----|------------------|--------------|-------|
| **210** (live dictator) | 1.26 / 11.9 GB | 20 | already minimal; ha-* total ~200 MB. Load 0.22. |
| **.245** (fileserver; temp HA stand-in) | 2.7 / 32 GB (29 GB cache) | 40 | **HANDS-OFF host.** ha-* are guests only. Load 0.00. |

## 210 (critical dictator) — leave essentially as-is
- **KEEP `bluetooth`** — the local `scanner.py` uses BlueZ for the radio (`aranet_radon`'s only source). Do NOT disable.
- **Only candidate — `ipvsadm`** (enabled, unused): keepalived here is **VRRP-only** (`virtual_server` blocks = 0),
  so IPVS/LVS is never used. Disabling just avoids loading the `ip_vs` module. Trivial benefit, reversible.
  - `sudo systemctl disable --now ipvsadm.service`   (rollback: `sudo systemctl enable --now ipvsadm.service`)
- Everything else is minimal — no changes warranted. *Execution: dev/ops have root on 210.*

## .245 — HANDS-OFF (no trims; reference profile only)
**Do not change the host.** .245 is a critical fileserver standing in temporarily for a dedicated HA box; the
`ha-*` services are guests we may bounce, nothing else. The earlier "cron 6 GB" was a red herring — just
`nzbget`'s I/O page-cache charged to cron's cgroup (RSS ~950 MB), not a leak.

*Reference only (for the FUTURE dedicated HA box, NOT for .245):* on a fresh bare-metal HA box, a lean base
omits cloud-init (no cloud datasource), dpdk (no userspace NIC), and multipathd (no SAN) — and keeps snapd if
anything (e.g. lxd) is a snap. Apply that when provisioning the dedicated box; never retrofit it onto .245.

## Expected payoff & risk
Effectively nil today: 210 is already lean and .245 is off-limits. Value is forward-looking — bake the lean base
into the dedicated box's provisioning when it's bought.

## Execution rights note
210's `ipvsadm` trim: dev/ops can do (root on 210). .245: **no execution** — host is hands-off.

## Status — dev review + execution (2026-06-24)
**dev agrees with this plan.** Independently verified the one actionable on 210 before applying: keepalived has
**0 `virtual_server` blocks** (VRRP-only), the IPVS table is **empty**, and `ip_vs` wasn't loaded at boot — so
`ipvsadm` is genuinely enabled-but-unused and is a separate service from keepalived (can't affect the VIP/control).
- **APPLIED:** `sudo systemctl disable --now ipvsadm.service` on 210 → disabled+inactive. Verified post-change:
  keepalived/ha-controller/ha-api still active, VIP `.200` + host `.210` still held. Reversible:
  `sudo systemctl enable --now ipvsadm.service`.
- **Endorse the `.245` retraction + hands-off guardrail** and the **KEEP `bluetooth`** call (the local `scanner.py`
  needs BlueZ for `aranet_radon` — disabling it would silently drop a sensor).
- **Refinement (forward):** capture the lean-base profile (omit cloud-init / dpdk / multipathd) in **`provisioning/`**
  (the server spec / a bare-metal profile) when the dedicated HA box is bought — so it's applied at provision time,
  not rediscovered from this decision doc.
